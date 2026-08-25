"""
local_tcp_client.py — Standalone Windows TCP/IP client.

This script allows a Windows computer on the same local network as the
Linux GPU workstation to send a CZI microscopy image to the μPiX server
without using ZEISS ZEN Blue.

For each selected CZI file, the client:

    1. Connects to the Linux TCP server.
    2. Sends the original CZI file using the project transfer protocol.
    3. Waits for the server to announce how many processed CZI files
       will be returned.
    4. Receives each denoised result and saves it locally.
    5. Closes the connection and optionally processes another file.

This client is useful for testing the image-denoising pipeline from any
compatible Windows computer connected to the local network.
"""

import os
import socket
import struct
import sys


# ----------------------------------------------------------
# NETWORK AND LOCAL CONFIGURATION
# ----------------------------------------------------------

# Replace these generic values with the configuration of your workstation.
BASE_INPUT_DIR = r"C:\path\to\input"
SAVE_DIR = r"C:\path\to\results"

SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000

NET_CHUNK_SIZE = 1024 * 1024


# ----------------------------------------------------------
# NETWORK FUNCTIONS
# ----------------------------------------------------------

def recv_exact(sock, n):
    """Receive exactly ``n`` bytes from the connected TCP socket.

    Args:
        sock (socket.socket): Connected TCP socket.
        n (int): Number of bytes to receive.

    Returns:
        bytes: Exactly ``n`` received bytes.

    Raises:
        ConnectionError: If the connection is closed before all expected
            bytes are received.
    """
    data = b""

    while len(data) < n:
        packet = sock.recv(n - len(data))

        if not packet:
            raise ConnectionError(
                "The connection was closed before all expected data was received."
            )

        data += packet

    return data


def send_czi_file(sock, filepath):
    """Send a CZI file using the protocol expected by the Linux server.

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

    name_size = len(filename_bytes)
    file_size = os.path.getsize(filepath)

    print(
        "\n[NETWORK] Preparing transfer: {} ({} bytes)".format(
            filename,
            file_size,
        )
    )

    # Send filename length.
    sock.sendall(
        struct.pack(
            "!I",
            name_size,
        )
    )

    # Send filename.
    sock.sendall(
        filename_bytes
    )

    # Send total file size.
    sock.sendall(
        struct.pack(
            "!Q",
            file_size,
        )
    )

    # Send file content in 1 MB chunks.
    sent = 0

    with open(filepath, "rb") as file_handle:
        while True:
            chunk = file_handle.read(
                NET_CHUNK_SIZE
            )

            if not chunk:
                break

            sock.sendall(
                chunk
            )

            sent += len(chunk)

            print(
                "\r[SEND] Progress: {}/{} bytes ({:.1f}%)".format(
                    sent,
                    file_size,
                    sent / float(file_size) * 100,
                ),
                end="",
            )

    print("\n[SEND] CZI file transmitted successfully.")


def receive_result_file(sock, save_dir):
    """Receive one denoised CZI file returned by the server.

    Args:
        sock (socket.socket): Connected TCP socket.
        save_dir (str): Local directory in which the result is stored.

    Returns:
        str: Local path of the received CZI file.
    """
    name_size = struct.unpack(
        "!I",
        recv_exact(sock, 4),
    )[0]

    filename = recv_exact(
        sock,
        name_size,
    ).decode("utf-8")

    filename = os.path.basename(
        filename
    )

    file_size = struct.unpack(
        "!Q",
        recv_exact(sock, 8),
    )[0]

    print(
        "[RETURN] Receiving result: {} ({} bytes)".format(
            filename,
            file_size,
        )
    )

    save_path = os.path.join(
        save_dir,
        filename,
    )

    received = 0

    with open(save_path, "wb") as file_handle:
        while received < file_size:
            chunk = sock.recv(
                min(
                    NET_CHUNK_SIZE,
                    file_size - received,
                )
            )

            if not chunk:
                raise ConnectionError(
                    "The connection was closed while receiving {}.".format(
                        filename
                    )
                )

            file_handle.write(
                chunk
            )

            received += len(chunk)

            print(
                "\r[RETURN] Received: {}/{} bytes".format(
                    received,
                    file_size,
                ),
                end="",
            )

    print(
        "\n[RETURN] File saved to: {}".format(
            save_path
        )
    )

    return save_path


# ----------------------------------------------------------
# FILE SELECTION
# ----------------------------------------------------------

def select_czi_file():
    """Interactively select a CZI file located in the configured input directory.

    Returns:
        tuple[str, str]: Full file path and selected filename.
    """
    available_directory = os.listdir(
        BASE_INPUT_DIR
    )

    while True:
        image_folder = input(
            "Folder containing the CZI image: "
        ).strip()

        full_folder_path = os.path.join(
            BASE_INPUT_DIR,
            image_folder,
        )

        if (
            image_folder not in available_directory
            or not os.path.isdir(full_folder_path)
        ):
            print(
                "[ERROR] Folder '{}' does not exist in the configured input directory. "
                "Please try again.".format(
                    image_folder
                )
            )
            continue

        break

    available_data = os.listdir(
        full_folder_path
    )

    while True:
        image_name = input(
            "CZI image name without the .czi extension: "
        ).strip()

        full_image_name = image_name + ".czi"

        if full_image_name not in available_data:
            print(
                "[ERROR] Image '{}' was not found. "
                "Please try again.".format(
                    full_image_name
                )
            )
            continue

        file_path = os.path.join(
            full_folder_path,
            full_image_name,
        )

        return file_path, full_image_name


# ----------------------------------------------------------
# MAIN PROGRAM
# ----------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(
        SAVE_DIR,
        exist_ok=True,
    )

    while True:
        print("=" * 60)
        print("    WINDOWS CLIENT — μPiX TCP/IP IMAGE DENOISING")
        print("=" * 60)

        file_path, full_image_name = select_czi_file()

        # One TCP connection is created for each CZI file.
        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        try:
            print(
                "\n[CONNECTION] Connecting to Linux server [{}:{}]...".format(
                    SERVER_IP,
                    SERVER_PORT,
                )
            )

            sock.connect(
                (
                    SERVER_IP,
                    SERVER_PORT,
                )
            )

            print(
                "[CONNECTION] Connected to the server."
            )

            # Step 1: Send the original CZI file.
            send_czi_file(
                sock,
                file_path,
            )

            # Step 2: Read the dynamic output count announced by the server.
            print(
                "\n[NETWORK] Waiting for server processing information..."
            )

            raw_num_outputs = recv_exact(
                sock,
                4,
            )

            num_outputs = struct.unpack(
                "!I",
                raw_num_outputs,
            )[0]

            print(
                "[NETWORK] Server announced {} denoised file(s).".format(
                    num_outputs
                )
            )

            # Step 3: Receive all processed outputs sequentially.
            received_results = []

            for index in range(num_outputs):
                print(
                    "\n--- Receiving processed file {}/{} ---".format(
                        index + 1,
                        num_outputs,
                    )
                )

                result_path = receive_result_file(
                    sock,
                    SAVE_DIR,
                )

                received_results.append(
                    result_path
                )

            print("\n" + "=" * 60)
            print("    IMAGE PROCESSING COMPLETED SUCCESSFULLY")
            print("=" * 60)
            print(
                "Original file: {}".format(
                    full_image_name
                )
            )

            for result_path in received_results:
                print(
                    " -> Processed output: {}".format(
                        os.path.basename(
                            result_path
                        )
                    )
                )

        except Exception as exc:
            print(
                "\n[ERROR] Client or network failure: {}".format(
                    exc
                )
            )

        finally:
            sock.close()
            print(
                "\n[CONNECTION] Socket closed."
            )

        process_again = input(
            "\nProcess another CZI file? [y/n]: "
        ).strip().lower()

        if process_again == "n":
            print(
                "[INFO] Closing the client application."
            )
            sys.exit()
