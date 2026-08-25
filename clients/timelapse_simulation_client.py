"""
timelapse_simulation_client.py — Time-lapse simulation client.

This script simulates the transmission of CZI time-lapse acquisitions to
the remote μPiX processing server.

It can operate in two modes:

    1. Batch mode:
       Select a number of image planes from an existing CZI time-lapse file,
       build a temporary CZI subset, and send the subset to the server in a
       single TCP/IP transaction.

    2. Live simulation mode:
       Replay an existing CZI time-lapse as if it were being acquired in
       real time. For each time point T and Z plane, the three channels are
       extracted, sent to the server, processed, and returned before the
       simulation continues.

The script is intended to test the TCP/IP image-processing workflow without
requiring a live ZEISS ZEN acquisition.
"""

import os
import re
import socket
import struct
import sys
import time

from pylibCZIrw import czi as pyczi


# ----------------------------------------------------------
# NETWORK AND LOCAL CONFIGURATION
# ----------------------------------------------------------

# Replace these generic values with the configuration of your workstation.
SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000

RESULT_DIR = r"C:\path\to\results"

NET_CHUNK_SIZE = 1024 * 1024
NET_TIMEOUT_S = 1800

NUM_CHANNELS = 3
DEFAULT_INTERVAL_S = 5.0


# ----------------------------------------------------------
# NETWORK FUNCTIONS
# ----------------------------------------------------------

def recv_exact(sock, n):
    """Receive exactly ``n`` bytes from a TCP socket.

    TCP does not guarantee that all requested bytes are returned by a
    single ``recv()`` call. This helper therefore keeps reading until
    the requested amount of data has been received.

    Args:
        sock (socket.socket): Connected TCP socket.
        n (int): Number of bytes to receive.

    Returns:
        bytes: Exactly ``n`` received bytes.

    Raises:
        ConnectionError: If the server closes the connection before all
            requested bytes are received.
    """
    data = b""
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            raise ConnectionError(
                "The server closed the connection before all data was received."
            )
        data += packet
    return data


def send_czi(sock, filepath):
    """Send one CZI file using the protocol expected by the Linux server.

    Transfer format:
        1. Filename length: 4-byte unsigned integer, big-endian.
        2. UTF-8 encoded filename.
        3. File size: 8-byte unsigned integer, big-endian.
        4. Raw file content sent in chunks.

    Args:
        sock (socket.socket): Connected TCP socket.
        filepath (str): Path to the CZI file to send.
    """
    filename = os.path.basename(filepath)
    filename_bytes = filename.encode("utf-8")
    file_size = os.path.getsize(filepath)

    sock.sendall(struct.pack("!I", len(filename_bytes)))
    sock.sendall(filename_bytes)
    sock.sendall(struct.pack("!Q", file_size))

    sent = 0
    with open(filepath, "rb") as file_handle:
        while sent < file_size:
            chunk = file_handle.read(min(NET_CHUNK_SIZE, file_size - sent))
            if not chunk:
                break

            sock.sendall(chunk)
            sent += len(chunk)

    print(
        "[SEND] {} transmitted ({:.2f} MB).".format(
            filename, file_size / (1024 * 1024)
        )
    )


def receive_result_file(sock, prefix_tag=""):
    """Receive one processed CZI file returned by the server.

    Args:
        sock (socket.socket): Connected TCP socket.
        prefix_tag (str): Optional prefix used to keep live-simulation
            results from different T/Z positions separate.

    Returns:
        str: Local path of the received CZI file.
    """
    name_size = struct.unpack("!I", recv_exact(sock, 4))[0]

    filename = recv_exact(sock, name_size).decode("utf-8")
    filename = os.path.basename(filename)

    if prefix_tag:
        filename = "{}_{}".format(prefix_tag, filename)

    file_size = struct.unpack("!Q", recv_exact(sock, 8))[0]

    output_path = os.path.join(RESULT_DIR, filename)
    received = 0

    with open(output_path, "wb") as file_handle:
        while received < file_size:
            chunk_size = min(NET_CHUNK_SIZE, file_size - received)
            chunk = recv_exact(sock, chunk_size)

            file_handle.write(chunk)
            received += len(chunk)

    print(
        "[RETURN] Received: {} ({:.2f} MB)".format(
            filename, file_size / (1024 * 1024)
        )
    )
    return output_path


def send_and_receive(subset_path, prefix_tag=""):
    """Open one TCP connection, send a CZI file, receive results, then close.

    Args:
        subset_path (str): CZI file to send.
        prefix_tag (str): Optional prefix added to returned filenames.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(NET_TIMEOUT_S)
    sock.connect((SERVER_IP, SERVER_PORT))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    try:
        send_czi(sock, subset_path)

        # The server first sends a 4-byte integer containing the number
        # of reconstructed CZI files that will be returned.
        num_files = struct.unpack("!I", recv_exact(sock, 4))[0]
        print("[RETURN] Server announced {} processed file(s).".format(num_files))

        for index in range(num_files):
            print(
                "[RETURN] Receiving file {}/{}...".format(
                    index + 1, num_files
                )
            )
            receive_result_file(sock, prefix_tag=prefix_tag)

    finally:
        sock.close()


# ----------------------------------------------------------
# CZI EXTRACTION FUNCTIONS
# ----------------------------------------------------------

def extract_subset_czi(source_path, planes, output_path):
    """Create a temporary CZI containing only the requested planes.

    Time and Z indices are re-indexed locally so that the generated subset
    starts from T=0 and Z=0, which matches the processing server workflow.

    Args:
        source_path (str): Original time-lapse CZI file.
        planes (list[dict]): Plane coordinates containing T, Z, and C.
        output_path (str): Destination path for the generated CZI subset.

    Returns:
        str: Path to the generated subset.
    """
    z_values = sorted({plane["Z"] for plane in planes})
    t_values = sorted({plane["T"] for plane in planes})

    z_local = {z_value: index for index, z_value in enumerate(z_values)}
    t_local = {t_value: index for index, t_value in enumerate(t_values)}

    with pyczi.open_czi(source_path) as src:
        with pyczi.create_czi(output_path, exist_ok=True) as dst:
            for plane in planes:
                src_coords = {
                    "T": plane["T"],
                    "Z": plane["Z"],
                    "C": plane["C"],
                }
                data = src.read(plane=src_coords)

                dst_coords = {
                    "T": t_local[plane["T"]],
                    "Z": z_local[plane["Z"]],
                    "C": plane["C"],
                }
                dst.write(data=data, plane=dst_coords)

    return output_path


def get_dimensions(source_path):
    """Read the number of channels, Z planes, and time points from a CZI."""
    with pyczi.open_czi(source_path) as czi_file:
        bounding_box = czi_file.total_bounding_box

        num_z = bounding_box.get("Z", (0, 1))[1]
        num_t = bounding_box.get("T", (0, 1))[1]
        num_c = bounding_box.get("C", (0, 1))[1]

    return num_c, num_z, num_t


def guess_real_interval_seconds(source_path):
    """Try to extract the real acquisition interval from CZI metadata.

    Args:
        source_path (str): Source CZI time-lapse file.

    Returns:
        float | None: Detected interval in seconds, or ``None`` if no
            compatible metadata entry is found.
    """
    try:
        with pyczi.open_czi(source_path) as czi_file:
            raw_xml = czi_file.raw_metadata
    except Exception:
        return None

    patterns = [
        r"<Interval>\s*<TimeSpan>\s*([\d.]+)\s*</TimeSpan>",
        r"<TimeSeriesSetup>.*?<Interval[^>]*>\s*([\d.]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, raw_xml, re.DOTALL)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue

    return None


# ----------------------------------------------------------
# MODE 1: BATCH SIMULATION
# ----------------------------------------------------------

def quantize_to_channels(n_requested, n_available):
    """Adjust a requested plane count to a multiple of the channel count."""
    quantized = (
        (n_requested + NUM_CHANNELS - 1) // NUM_CHANNELS
    ) * NUM_CHANNELS

    quantized = min(quantized, n_available)
    quantized = max(quantized, NUM_CHANNELS)

    return quantized


def build_plane_list(num_z, num_t, num_channels):
    """Build an ordered list of all T/Z/C planes in the source acquisition."""
    planes = []

    for t_index in range(num_t):
        for z_index in range(num_z):
            for channel_index in range(num_channels):
                planes.append(
                    {
                        "T": t_index,
                        "Z": z_index,
                        "C": channel_index,
                    }
                )

    return planes


def run_batch_mode(source_path, num_c, num_z, num_t):
    """Create one CZI subset and send it to the server in a single request."""
    del num_c  # Kept in the signature for consistency with live mode.

    total_available_planes = NUM_CHANNELS * num_z * num_t

    n_requested = int(
        input(
            "Number of image planes to process "
            "(will be adjusted to a multiple of {}): ".format(NUM_CHANNELS)
        )
    )

    n_quantized = quantize_to_channels(
        n_requested,
        total_available_planes,
    )

    if n_quantized != n_requested:
        print(
            "[INFO] Requested value adjusted: {} -> {} "
            "(maximum {} available plane(s)).".format(
                n_requested,
                n_quantized,
                total_available_planes,
            )
        )

    all_planes = build_plane_list(num_z, num_t, NUM_CHANNELS)
    selected_planes = all_planes[:n_quantized]

    subset_path = os.path.join(
        RESULT_DIR,
        "subset_" + os.path.basename(source_path),
    )

    extract_subset_czi(
        source_path,
        selected_planes,
        subset_path,
    )

    print("[INFO] CZI subset created: {}".format(subset_path))
    print(
        "\n[CONNECTION] Sending to server {}:{}...".format(
            SERVER_IP,
            SERVER_PORT,
        )
    )

    try:
        send_and_receive(
            subset_path,
            prefix_tag="batch",
        )
    finally:
        if os.path.exists(subset_path):
            os.remove(subset_path)

    print("\n=== Batch simulation completed ===")


# ----------------------------------------------------------
# MODE 2: LIVE TIME-LAPSE SIMULATION
# ----------------------------------------------------------

def run_live_mode(source_path, num_c, num_z, num_t):
    """Replay a stored CZI as a simulated live time-lapse acquisition."""
    del num_c  # Kept in the signature for consistency with batch mode.

    interval = guess_real_interval_seconds(source_path)

    if interval is not None:
        print(
            "[INFO] Time interval detected in CZI metadata: {:.2f} s".format(
                interval
            )
        )

        use_detected = input(
            "Use the detected interval? (y/n, default y): "
        ).strip().lower()

        if use_detected == "n":
            interval = None

    if interval is None:
        raw_interval = input(
            "Interval between two T positions in seconds "
            "(default {} s): ".format(DEFAULT_INTERVAL_S)
        ).strip()

        interval = (
            float(raw_interval)
            if raw_interval
            else DEFAULT_INTERVAL_S
        )

    print(
        "[INFO] Live simulation: {} T position(s), {} Z level(s), "
        "interval {:.2f} s.".format(
            num_t,
            num_z,
            interval,
        )
    )
    print(
        "[INFO] Each Z level containing the three channels is sent "
        "as soon as it is simulated.\n"
    )

    for t_index in range(num_t):
        if t_index > 0:
            print(
                "\n[ACQUISITION] Waiting {:.2f} s before T={}...".format(
                    interval,
                    t_index,
                )
            )
            time.sleep(interval)

        for z_index in range(num_z):
            planes = [
                {
                    "T": t_index,
                    "Z": z_index,
                    "C": channel_index,
                }
                for channel_index in range(NUM_CHANNELS)
            ]

            subset_name = "live_T{}_Z{}_{}.czi".format(
                t_index,
                z_index,
                os.path.splitext(
                    os.path.basename(source_path)
                )[0],
            )

            subset_path = os.path.join(
                RESULT_DIR,
                subset_name,
            )

            extract_subset_czi(
                source_path,
                planes,
                subset_path,
            )

            print(
                "[ACQUISITION] T={} Z={} simulated "
                "(3 channels) -> sending to server...".format(
                    t_index,
                    z_index,
                )
            )

            try:
                send_and_receive(
                    subset_path,
                    prefix_tag="T{}_Z{}".format(
                        t_index,
                        z_index,
                    ),
                )
            except Exception as exc:
                print(
                    "[ERROR] Failed to process T={} Z={}: {}".format(
                        t_index,
                        z_index,
                        exc,
                    )
                )
            finally:
                if os.path.exists(subset_path):
                    os.remove(subset_path)

    print("\n=== Live acquisition simulation completed ===")


# ----------------------------------------------------------
# MAIN PROGRAM
# ----------------------------------------------------------

def main():
    """Run the interactive time-lapse simulation client."""
    while True:
        try:
            os.makedirs(
                RESULT_DIR,
                exist_ok=True,
            )

            source_path = input(
                "Path to the source time-lapse CZI file: "
            ).strip().strip('"')

            if not os.path.isfile(source_path):
                print(
                    "[ERROR] File not found: {}".format(
                        source_path
                    )
                )
                sys.exit(1)

            num_c, num_z, num_t = get_dimensions(
                source_path
            )

            print(
                "[INFO] Source file -> Channels: {} | "
                "Z-stacks: {} | Time points: {}".format(
                    num_c,
                    num_z,
                    num_t,
                )
            )

            if num_c < NUM_CHANNELS:
                print(
                    "[ERROR] The source contains only {} channel(s); "
                    "{} are required.".format(
                        num_c,
                        NUM_CHANNELS,
                    )
                )
                sys.exit(1)

            print("\nAvailable modes:")
            print(
                "  1) Batch - select a number of planes "
                "and send them together"
            )
            print(
                "  2) Live  - replay the file as a live "
                "time-lapse acquisition"
            )

            mode = input("Select mode (1/2): ").strip()

            if mode == "2":
                run_live_mode(
                    source_path,
                    num_c,
                    num_z,
                    num_t,
                )
            else:
                run_batch_mode(
                    source_path,
                    num_c,
                    num_z,
                    num_t,
                )

        except KeyboardInterrupt:
            print("\n[INFO] Stop requested.")


if __name__ == "__main__":
    main()
