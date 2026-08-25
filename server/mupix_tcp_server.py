"""
mupix_tcp_server.py — Linux GPU processing server for μPiX.

This script is the server-side component of the Smart Microscopy Image
Denoising project. It runs continuously on a Linux workstation equipped
with a GPU and waits for TCP/IP connections from compatible clients.

For each received CZI microscopy image, the server:

    1. Reads the CZI dimensions and available channels.
    2. Splits each image plane into fixed-size patches.
    3. Runs μPiX inference using the model associated with each wavelength.
    4. Reconstructs the denoised image from the predicted patches.
    5. Writes reconstructed CZI files.
    6. Sends all processed files back to the client through the same
       TCP connection.

The current implementation is configured for three fluorescence channels:
488 nm, 561 nm, and 640 nm.

External dependencies:
    - numpy
    - tifffile
    - pylibCZIrw
    - aicspylibczi
    - μPiX project, providing ``initialiser_et_predire``

Before running the script, update the configuration paths below.
"""

import gc
import glob
import os
import shutil
import socket
import struct
import sys
import time
import traceback

import numpy as np
import tifffile
from aicspylibczi import CziFile
from pylibCZIrw import czi as pyczi


# ----------------------------------------------------------
# MUPIX IMPORT
# ----------------------------------------------------------

# μPiX is not imported as a standard installed package in the original
# development environment. The project directory is therefore added to
# ``sys.path`` before importing the inference function.
#
# IMPORTANT:
# Replace this path with the local μPiX installation directory.
MUPIX_DIR = "/path/to/mupix"

if MUPIX_DIR not in sys.path:
    sys.path.append(MUPIX_DIR)

from mupixinfer import initialiser_et_predire


# ----------------------------------------------------------
# SERVER AND PROCESSING CONFIGURATION
# ----------------------------------------------------------

# Listen on all available network interfaces.
LISTEN_IP = "0.0.0.0"

# TCP port shared by the server and all compatible clients.
LISTEN_PORT = 5000

# Working directory used for received CZI files and temporary TIFF patches.
SAVE_DIR = "/path/to/input_data"

# Directory used for reconstructed denoised CZI files.
RESULT_DIR = "/path/to/results"

# Fluorescence wavelengths processed by the current μPiX workflow.
CHANNEL_WAVELENGTHS = [488, 561, 640]

# Size of patches extracted before μPiX inference.
PATCH_SIZE = 512

# Network transfer chunk size: 1 MB.
NET_CHUNK_SIZE = 1024 * 1024

os.makedirs(SAVE_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


# ----------------------------------------------------------
# NETWORK FUNCTIONS
# ----------------------------------------------------------

def recv_exact(conn, n):
    """Receive exactly ``n`` bytes from a TCP connection.

    TCP does not guarantee that a single ``recv()`` call returns all
    requested bytes. This helper therefore keeps reading until the
    requested number of bytes has been received.

    Args:
        conn (socket.socket): Active TCP connection.
        n (int): Number of bytes to receive.

    Returns:
        bytes: Exactly ``n`` bytes.

    Raises:
        ConnectionError: If the client closes the connection before all
            expected bytes are received.
    """
    data = b""

    while len(data) < n:
        packet = conn.recv(n - len(data))

        if not packet:
            raise ConnectionError(
                "The client closed the connection before all expected data was received."
            )

        data += packet

    return data


def receive_czi(conn):
    """Receive one CZI file from a connected client and save it locally.

    Expected transfer format:

        1. Filename length: 4-byte unsigned integer, big-endian.
        2. UTF-8 encoded filename.
        3. File size: 8-byte unsigned integer, big-endian.
        4. Raw file content sent in chunks.

    Args:
        conn (socket.socket): Active TCP connection.

    Returns:
        tuple[str, str] | tuple[None, None]:
            Local file path and filename, or ``(None, None)`` when the
            client closes the connection before sending another file.
    """
    raw_size = conn.recv(4)

    if not raw_size:
        return None, None

    if len(raw_size) < 4:
        raw_size += recv_exact(conn, 4 - len(raw_size))

    name_size = struct.unpack("!I", raw_size)[0]

    filename = recv_exact(conn, name_size).decode("utf-8")
    filename = os.path.basename(filename)

    file_size = struct.unpack(
        "!Q",
        recv_exact(conn, 8),
    )[0]

    print(
        "\n[NETWORK] Receiving: {} ({:.2f} MB)".format(
            filename,
            file_size / (1024 * 1024),
        )
    )

    save_path = os.path.join(
        SAVE_DIR,
        filename,
    )

    received = 0
    start_time = time.perf_counter()

    with open(save_path, "wb") as file_handle:
        while received < file_size:
            chunk = conn.recv(
                min(
                    NET_CHUNK_SIZE,
                    file_size - received,
                )
            )

            if not chunk:
                raise ConnectionError(
                    "The connection was closed while receiving the file."
                )

            file_handle.write(chunk)
            received += len(chunk)

            print(
                "\r[NETWORK] Received: {:.1f}%".format(
                    received / float(file_size) * 100
                ),
                end="",
            )

    elapsed = time.perf_counter() - start_time

    print(
        "\n[NETWORK] Saved in {:.3f} s: {}".format(
            elapsed,
            save_path,
        )
    )

    return save_path, filename


def send_file_on_same_connection(conn, file_path, filename):
    """Send one processed file back to the client.

    The output transfer format matches the input protocol:

        1. Filename length: 4-byte unsigned integer, big-endian.
        2. UTF-8 encoded filename.
        3. File size: 8-byte unsigned integer, big-endian.
        4. Raw file content.

    Args:
        conn (socket.socket): Active TCP connection.
        file_path (str): Local path of the file to send.
        filename (str): Filename announced to the client.

    Raises:
        FileNotFoundError: If the output file does not exist.
        ConnectionError: If the network transfer fails.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(
            "Cannot send '{}': file does not exist at {}".format(
                filename,
                file_path,
            )
        )

    filename_bytes = filename.encode("utf-8")
    file_size = os.path.getsize(file_path)

    print(
        "[RETURN] Sending processed file: {}".format(
            filename
        )
    )

    start_time = time.perf_counter()

    try:
        conn.sendall(
            struct.pack(
                "!I",
                len(filename_bytes),
            )
        )

        conn.sendall(
            filename_bytes
        )

        conn.sendall(
            struct.pack(
                "!Q",
                file_size,
            )
        )

        sent = 0

        with open(file_path, "rb") as file_handle:
            while sent < file_size:
                chunk = file_handle.read(
                    min(
                        NET_CHUNK_SIZE,
                        file_size - sent,
                    )
                )

                if not chunk:
                    raise ConnectionError(
                        "File reading stopped before transfer completion."
                    )

                conn.sendall(chunk)
                sent += len(chunk)

    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        raise ConnectionError(
            "Failed to send '{}': {}".format(
                filename,
                exc,
            )
        ) from exc

    elapsed = time.perf_counter() - start_time

    print(
        "[RETURN] {} sent in {:.3f} s ({:.2f} MB).".format(
            filename,
            elapsed,
            file_size / (1024 * 1024),
        )
    )


# ----------------------------------------------------------
# MUPIX MODEL AND WORKING-DIRECTORY MANAGEMENT
# ----------------------------------------------------------

def get_model_paths(wavelength):
    """Return μPiX experiment paths for one fluorescence wavelength.

    Args:
        wavelength (int): Wavelength in nanometers.

    Returns:
        tuple[str, str, str]:
            Experiment directory, prediction directory, and test directory.

    Raises:
        ValueError: If the wavelength is not configured.
    """
    experiment_base = os.path.join(
        MUPIX_DIR,
        "experiments",
    )

    if wavelength not in CHANNEL_WAVELENGTHS:
        raise ValueError(
            "Unsupported wavelength: {}".format(
                wavelength
            )
        )

    experiment_path = os.path.join(
        experiment_base,
        "metrology_experiment_{}_2".format(
            wavelength
        ),
    )

    prediction_dir = os.path.join(
        experiment_path,
        "predictions",
    )

    test_dir = os.path.join(
        MUPIX_DIR,
        "metrology_{}_2".format(
            wavelength
        ),
        "test",
    )

    return experiment_path, prediction_dir, test_dir


def clean_folder(folder):
    """Remove all files and subdirectories inside a working directory."""
    if not os.path.exists(folder):
        return

    for path in glob.glob(
        os.path.join(
            folder,
            "*",
        )
    ):
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)

        except Exception as exc:
            print(
                "[CLEANUP] Could not remove {}: {}".format(
                    path,
                    exc,
                )
            )


# ----------------------------------------------------------
# PATCH EXTRACTION AND RECONSTRUCTION
# ----------------------------------------------------------

def dim_kwargs(czi_reader, **wanted):
    """Keep only CZI dimensions that are present in the current file."""
    return {
        key: value
        for key, value in wanted.items()
        if key in czi_reader.dims
    }


def split_into_patches(image, patch_size):
    """Split a 2D image into square patches.

    Symmetric padding is applied when the image dimensions are not exact
    multiples of the patch size.

    Args:
        image (np.ndarray): 2D input image.
        patch_size (int): Square patch size in pixels.

    Returns:
        tuple[list[dict], int, int]:
            Patch list, padded height, and padded width.
    """
    height, width = image.shape

    padded_height = (
        (height + patch_size - 1) // patch_size
    ) * patch_size

    padded_width = (
        (width + patch_size - 1) // patch_size
    ) * patch_size

    pad_height = padded_height - height
    pad_width = padded_width - width

    if pad_height or pad_width:
        image = np.pad(
            image,
            (
                (0, pad_height),
                (0, pad_width),
            ),
            mode="symmetric",
        )

    patches = []

    for y_pos in range(
        0,
        padded_height,
        patch_size,
    ):
        for x_pos in range(
            0,
            padded_width,
            patch_size,
        ):
            patches.append(
                {
                    "x": x_pos,
                    "y": y_pos,
                    "data": image[
                        y_pos:y_pos + patch_size,
                        x_pos:x_pos + patch_size,
                    ],
                }
            )

    return patches, padded_height, padded_width


def stitch_patches(
    patch_arrays,
    padded_height,
    padded_width,
    original_height,
    original_width,
    patch_size,
):
    """Reconstruct a complete image from predicted patches."""
    canvas = np.zeros(
        (
            padded_height,
            padded_width,
        ),
        dtype=patch_arrays[0]["data"].dtype,
    )

    for patch in patch_arrays:
        canvas[
            patch["y"]:patch["y"] + patch_size,
            patch["x"]:patch["x"] + patch_size,
        ] = patch["data"]

    return canvas[
        :original_height,
        :original_width,
    ]


# ----------------------------------------------------------
# MAIN SERVER LOOP
# ----------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  LINUX GPU SERVER — μPiX IMAGE DENOISING")
    print("=" * 60)

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1,
    )

    server.bind(
        (
            LISTEN_IP,
            LISTEN_PORT,
        )
    )

    server.listen(5)

    print(
        "[INFO] Server listening on port {}...".format(
            LISTEN_PORT
        )
    )

    try:
        while True:
            print(
                "\n[INFO] Waiting for a client connection...\n"
            )

            conn, addr = server.accept()

            print(
                "[NETWORK] Connected client: {}".format(
                    addr
                )
            )

            conn.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_KEEPALIVE,
                1,
            )

            while True:
                test_dirs_to_clean = set()

                try:
                    # --------------------------------------------------
                    # STEP 1: RECEIVE THE ORIGINAL CZI FILE
                    # --------------------------------------------------

                    czi_path, filename = receive_czi(
                        conn
                    )

                    if czi_path is None:
                        print(
                            "[NETWORK] Client closed the session."
                        )
                        break

                    total_start = time.perf_counter()

                    base_name = os.path.splitext(
                        filename
                    )[0]

                    pipeline_tasks = {
                        wavelength: []
                        for wavelength in CHANNEL_WAVELENGTHS
                    }

                    # --------------------------------------------------
                    # STEP 2: ANALYZE THE CZI AND EXTRACT PATCHES
                    # --------------------------------------------------

                    prep_start = time.perf_counter()

                    print(
                        "\n[PROCESSING] Reading CZI geometry "
                        "and extracting patches..."
                    )

                    czi_reader = CziFile(
                        czi_path
                    )

                    raw_xml = getattr(
                        czi_reader,
                        "meta",
                        None,
                    )

                    is_mosaic = czi_reader.is_mosaic()

                    dims_shape = czi_reader.get_dims_shape()[0]

                    num_z = dims_shape.get(
                        "Z",
                        (0, 1),
                    )[1]

                    num_m = (
                        dims_shape.get(
                            "M",
                            (0, 1),
                        )[1]
                        if is_mosaic
                        else 1
                    )

                    num_channels_available = dims_shape.get(
                        "C",
                        (0, 1),
                    )[1]

                    print(
                        "[INFO] Dimensions -> Z: {} | Mosaic M: {} | Channels: {}".format(
                            num_z,
                            num_m,
                            num_channels_available,
                        )
                    )

                    total_patch_count = 0

                    for channel_index, wavelength in enumerate(
                        CHANNEL_WAVELENGTHS
                    ):
                        (
                            experiment_path,
                            prediction_dir,
                            test_dir,
                        ) = get_model_paths(
                            wavelength
                        )

                        test_dirs_to_clean.add(
                            test_dir
                        )

                        clean_folder(
                            test_dir
                        )

                        # If fewer channels are available, read channel 0
                        # as a fallback to preserve compatibility with
                        # single-channel CZI extracts.
                        channel_to_read = (
                            channel_index
                            if channel_index < num_channels_available
                            else 0
                        )

                        for mosaic_index in range(
                            num_m
                        ):
                            tile_x = 0
                            tile_y = 0

                            if is_mosaic:
                                bbox_kwargs = dim_kwargs(
                                    czi_reader,
                                    S=0,
                                    T=0,
                                    C=channel_to_read,
                                    M=mosaic_index,
                                )

                                tile_bbox = czi_reader.get_mosaic_tile_bounding_box(
                                    **bbox_kwargs
                                )

                                tile_x = tile_bbox.x
                                tile_y = tile_bbox.y

                            for z_index in range(
                                num_z
                            ):
                                read_kwargs = dim_kwargs(
                                    czi_reader,
                                    S=0,
                                    T=0,
                                    C=channel_to_read,
                                    Z=z_index,
                                    M=mosaic_index,
                                )

                                image_data, _ = czi_reader.read_image(
                                    **read_kwargs
                                )

                                image = np.squeeze(
                                    image_data
                                )

                                original_height, original_width = image.shape

                                (
                                    patches,
                                    padded_height,
                                    padded_width,
                                ) = split_into_patches(
                                    image,
                                    PATCH_SIZE,
                                )

                                total_patch_count += len(
                                    patches
                                )

                                patch_tasks = []

                                for patch in patches:
                                    patch_name = (
                                        "{}_ch{}_m{}_z{}_px{}_py{}_{}.tif"
                                    ).format(
                                        base_name,
                                        channel_index,
                                        mosaic_index,
                                        z_index,
                                        patch["x"],
                                        patch["y"],
                                        wavelength,
                                    )

                                    patch_path = os.path.join(
                                        SAVE_DIR,
                                        patch_name,
                                    )

                                    tifffile.imwrite(
                                        patch_path,
                                        patch["data"],
                                    )

                                    shutil.copy2(
                                        patch_path,
                                        os.path.join(
                                            test_dir,
                                            patch_name,
                                        ),
                                    )

                                    patch_tasks.append(
                                        {
                                            "x": patch["x"],
                                            "y": patch["y"],
                                            "tiff_name": patch_name,
                                        }
                                    )

                                pipeline_tasks[wavelength].append(
                                    {
                                        "m_idx": mosaic_index,
                                        "z_idx": z_index,
                                        "pred_dir": prediction_dir,
                                        "exp_path": experiment_path,
                                        "tile_x": tile_x,
                                        "tile_y": tile_y,
                                        "orig_h": original_height,
                                        "orig_w": original_width,
                                        "padded_h": padded_height,
                                        "padded_w": padded_width,
                                        "patches": patch_tasks,
                                    }
                                )

                    if hasattr(
                        czi_reader,
                        "close",
                    ):
                        czi_reader.close()

                    del czi_reader
                    gc.collect()

                    prep_elapsed = (
                        time.perf_counter()
                        - prep_start
                    )

                    print(
                        "[TIMING] Patch extraction completed: "
                        "{} patches in {:.3f} s".format(
                            total_patch_count,
                            prep_elapsed,
                        )
                    )

                    # --------------------------------------------------
                    # STEP 3: RUN MUPIX GPU INFERENCE
                    # --------------------------------------------------

                    inference_start = time.perf_counter()

                    for wavelength in CHANNEL_WAVELENGTHS:
                        if not pipeline_tasks[wavelength]:
                            continue

                        sample_task = pipeline_tasks[wavelength][0]

                        print(
                            "\n[MUPIX] Starting GPU inference "
                            "for {} nm...".format(
                                wavelength
                            )
                        )

                        wavelength_start = time.perf_counter()

                        initialiser_et_predire(
                            sample_task["exp_path"]
                        )

                        wavelength_elapsed = (
                            time.perf_counter()
                            - wavelength_start
                        )

                        print(
                            "[TIMING] {} nm inference completed "
                            "in {:.3f} s".format(
                                wavelength,
                                wavelength_elapsed,
                            )
                        )

                    inference_elapsed = (
                        time.perf_counter()
                        - inference_start
                    )

                    print(
                        "\n[TIMING] Total GPU inference time: {:.3f} s".format(
                            inference_elapsed
                        )
                    )

                    # --------------------------------------------------
                    # STEP 4: RECONSTRUCT AND RETURN PROCESSED CZI FILES
                    # --------------------------------------------------

                    reconstruction_start = time.perf_counter()

                    total_files_to_send = sum(
                        len(tasks)
                        for tasks in pipeline_tasks.values()
                    )

                    # Inform the client how many CZI outputs will follow.
                    conn.sendall(
                        struct.pack(
                            "!I",
                            total_files_to_send,
                        )
                    )

                    print(
                        "\n[NETWORK] Announced {} output file(s) "
                        "to the client.".format(
                            total_files_to_send
                        )
                    )

                    for channel_index, wavelength in enumerate(
                        CHANNEL_WAVELENGTHS
                    ):
                        tasks = pipeline_tasks[
                            wavelength
                        ]

                        for task in tasks:
                            z_index = task["z_idx"]

                            frame_start = time.perf_counter()

                            output_czi_name = (
                                "{}_ch{}_z{}_{}_denoised.czi"
                            ).format(
                                base_name,
                                channel_index,
                                z_index,
                                wavelength,
                            )

                            output_czi_path = os.path.join(
                                RESULT_DIR,
                                output_czi_name,
                            )

                            patch_arrays = []

                            for patch in task["patches"]:
                                expected_prediction_path = os.path.join(
                                    task["pred_dir"],
                                    patch["tiff_name"],
                                )

                                if not os.path.exists(
                                    expected_prediction_path
                                ):
                                    raise FileNotFoundError(
                                        "Missing μPiX prediction: {}".format(
                                            patch["tiff_name"]
                                        )
                                    )

                                patch_image = tifffile.imread(
                                    expected_prediction_path
                                )

                                if patch_image.ndim == 3:
                                    patch_image = np.squeeze(
                                        patch_image
                                    )

                                patch_arrays.append(
                                    {
                                        "x": patch["x"],
                                        "y": patch["y"],
                                        "data": patch_image,
                                    }
                                )

                            reconstructed = stitch_patches(
                                patch_arrays,
                                task["padded_h"],
                                task["padded_w"],
                                task["orig_h"],
                                task["orig_w"],
                                PATCH_SIZE,
                            )

                            with pyczi.create_czi(
                                output_czi_path,
                                exist_ok=True,
                            ) as destination:
                                destination.write(
                                    data=reconstructed,
                                    plane={
                                        "T": 0,
                                        "Z": 0,
                                        "C": 0,
                                    },
                                    location=(
                                        task["tile_x"],
                                        task["tile_y"],
                                    ),
                                )

                                if (
                                    raw_xml is not None
                                    and hasattr(
                                        destination,
                                        "write_metadata",
                                    )
                                ):
                                    try:
                                        destination.write_metadata(
                                            raw_xml
                                        )
                                    except Exception:
                                        # Metadata writing is optional and
                                        # must not stop image reconstruction.
                                        pass

                            send_file_on_same_connection(
                                conn,
                                output_czi_path,
                                output_czi_name,
                            )

                            frame_elapsed = (
                                time.perf_counter()
                                - frame_start
                            )

                            print(
                                "[TIMING] Channel {} | Z={} "
                                "reconstructed and sent in {:.3f} s".format(
                                    channel_index,
                                    z_index,
                                    frame_elapsed,
                                )
                            )

                    reconstruction_elapsed = (
                        time.perf_counter()
                        - reconstruction_start
                    )

                    total_elapsed = (
                        time.perf_counter()
                        - total_start
                    )

                    print("\n" + "=" * 60)
                    print("  PROCESSING TIME SUMMARY")
                    print("=" * 60)
                    print(
                        "  Total execution time: {:.3f} s".format(
                            total_elapsed
                        )
                    )
                    print(
                        "  Reconstruction/return time: {:.3f} s".format(
                            reconstruction_elapsed
                        )
                    )
                    print("=" * 60)

                except (ConnectionError, BrokenPipeError) as exc:
                    print(
                        "\n[NETWORK] Client connection interrupted: {}".format(
                            exc
                        )
                    )
                    break

                except Exception as exc:
                    print(
                        "\n[SERVER ERROR] Processing failed: {}".format(
                            exc
                        )
                    )

                    traceback.print_exc()

                    # Notify the client that no result files will be returned.
                    try:
                        conn.sendall(
                            struct.pack(
                                "!I",
                                0,
                            )
                        )
                    except Exception:
                        pass

                    break

                finally:
                    # Always clean μPiX test directories after each request.
                    for test_dir in test_dirs_to_clean:
                        clean_folder(
                            test_dir
                        )

                    gc.collect()

            conn.close()

            print(
                "[NETWORK] Connection closed. Waiting for the next client."
            )

    except KeyboardInterrupt:
        print(
            "\n[INFO] Server stopped by user."
        )

    finally:
        server.close()
