# Smart Microscopy Image Denoising

## TCP/IP-Based Image Denoising for ZEISS ZEN Acquisitions

This repository contains the TCP/IP communication components developed for a smart microscopy image-denoising workflow.

The project connects microscopy acquisition or simulation clients to a Linux GPU workstation running the **[μPiX](https://gitlab.lis-lab.fr/sicomp/mupix)** image-denoising pipeline.

A client sends a CZI microscopy image to the Linux server. The server processes the image using the appropriate μPiX model, reconstructs the denoised CZI output, and returns the processed file or files through the same TCP/IP connection.

---

## System Architecture

```text
                         Local network
                              |
          +-------------------+-------------------+
          |                   |                   |
          v                   v                   v
 Time-lapse Simulation   Standalone Client   ZEISS ZEN Client
       Client              Windows PC        Microscope PC
          |                   |                   |
          +-------------------+-------------------+
                              |
                            TCP/IP
                              |
                              v
                     Linux GPU Server
                              |
                              v
                            μPiX
                              |
                              v
                       GPU Inference
                              |
                              v
                   Reconstructed CZI Files
                              |
                              v
                     Returned to Client
```

---

## Repository Structure

```text
Smart-Microscopy-Image-denoising/
│
├── server/
│   └── mupix_tcp_server.py
│
├── clients/
│   ├── timelapse_simulation_client.py
│   ├── local_tcp_client.py
│   └── zen_blue_client.py
│
├── README.md
├── requirements.txt
└── .gitignore
```

---

# 1. Linux GPU Server

File:

```text
server/mupix_tcp_server.py
```

The Linux server is the central processing component of the project.

It listens for TCP/IP connections, receives CZI microscopy images, splits the image data into patches, performs μPiX GPU inference, reconstructs the processed images, and sends the denoised CZI outputs back to the connected client.

The current server configuration uses:

```python
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 5000
```

The server supports the three fluorescence channels used in this project:

| Channel | Wavelength |
|---|---:|
| Channel 0 | 488 nm |
| Channel 1 | 561 nm |
| Channel 2 | 640 nm |

Each wavelength is processed using its corresponding μPiX experiment.

### Main server workflow

```text
Receive CZI
    |
    v
Read CZI dimensions
    |
    v
Split image into patches
    |
    v
μPiX GPU inference
    |
    v
Reconstruct image
    |
    v
Create denoised CZI output
    |
    v
Return result to client
```

---

# 2. Time-Lapse Simulation Client

File:

```text
clients/timelapse_simulation_client.py
```

This client is used to simulate time-lapse microscopy acquisition from an existing CZI file.

It makes it possible to test the complete TCP/IP denoising workflow without running a live acquisition in ZEISS ZEN Blue.

The client supports two operating modes.

## Batch Mode

Batch mode allows the user to select a number of image planes from an existing time-lapse CZI file.

The selected planes are copied into a temporary CZI file and sent to the Linux server in one TCP/IP transaction.

```text
Original Time-Lapse CZI
        |
        v
Select image planes
        |
        v
Create temporary CZI subset
        |
        v
Send to Linux server
        |
        v
μPiX processing
        |
        v
Receive denoised results
```

The requested number of planes is adjusted to a multiple of three because the current processing workflow expects the three fluorescence channels.

## Live Simulation Mode

Live mode replays an existing CZI file as if it were being acquired in real time.

For each time point `T` and each Z plane:

1. The three channels are extracted.
2. A temporary CZI subset is created.
3. The subset is sent to the server.
4. The processed results are received.
5. The client continues to the next Z plane or time point.

When possible, the script attempts to read the original acquisition interval from the CZI metadata.

If no interval is detected, the user can enter one manually.

This mode is useful for evaluating the behavior of the processing pipeline under conditions that are closer to real acquisition timing.

---

# 3. Standalone Local TCP Client

File:

```text
clients/local_tcp_client.py
```

It provides a simple way to send a CZI file to the Linux μPiX server from a Windows computer connected to the same local network.

It does not require ZEISS ZEN Blue.

### Workflow

```text
Select CZI file
      |
      v
Connect to Linux server
      |
      v
Send original CZI
      |
      v
Wait for μPiX processing
      |
      v
Receive output count
      |
      v
Receive denoised CZI files
      |
      v
Save results locally
```

The client establishes one TCP connection per selected CZI file.

After the original image is sent, the server returns a 4-byte integer indicating how many processed CZI files will follow.

The client then receives each result sequentially and saves it to the configured output directory.

### Main configuration

The following values must be adapted to the target workstation:

```python
BASE_INPUT_DIR = r"C:\path\to\input"
SAVE_DIR = r"C:\path\to\results"

SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000
```

The server must already be running and accessible through the local network before starting the client.

---

# 4. ZEISS ZEN Blue Client

File:

```text
clients/zen_blue_client.py
```

It is an **IronPython macro executed directly inside ZEISS ZEN Blue using the OAD environment**.

Unlike the standalone TCP client, this script is integrated into the microscope acquisition environment.

The macro can operate in two modes.

## Manual Mode

Manual mode processes a CZI image that is already open in ZEN Blue.

The user selects the document from the macro interface, and the image is saved locally before being transferred to the Linux processing server.

## Automatic Mode

Automatic mode starts a new microscopy acquisition using a predefined ZEN experiment:

```python
EXP_NAME = "ZEISS - LSM"
```

The acquired image is then automatically saved and sent to the Linux server.

### ZEN client workflow

```text
Manual selection
       OR
Automatic acquisition
        |
        v
Save CZI locally
        |
        v
Open TCP connection
        |
        v
Send CZI to Linux server
        |
        v
μPiX GPU processing
        |
        v
Receive denoised CZI files
        |
        v
Load results in ZEN Blue
```

Each returned result is automatically loaded into the ZEN Blue document workspace.

### Requirements

The ZEISS ZEN client requires:

- ZEISS ZEN Blue;
- OAD / macro support;
- access to the ZEN IronPython environment;
- TCP/IP connectivity to the Linux μPiX server.

The script cannot be executed with a standard Python interpreter because it depends on objects provided by ZEN Blue and the .NET runtime.

### Configuration

The following parameters must be adapted to the microscope workstation:

```python
EXP_NAME = "ZEISS - LSM"

SAVE_PATH = r"C:\path\to\temporary"
SAVE_DIR = r"C:\path\to\results"

SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000
```

---

# Validated Laser Intensities

The ZEN Blue client displays the minimum laser intensities used during the validation of the μPiX models in this project.

| Wavelength | Minimum validated laser intensity |
|---:|---:|
| 488 nm | 3.0% |
| 561 nm | 1.3% |
| 640 nm | 0.7% |

These values describe the validation conditions used in the project. They should not be interpreted as universal operating limits for μPiX outside this specific validation setup.

---

# TCP/IP Transfer Protocol

All three clients communicate with the Linux server using the same file-transfer structure.

## Client to Server

```text
4 bytes
Filename length
      |
      v
Filename
UTF-8
      |
      v
8 bytes
File size
      |
      v
Raw CZI file data
```

The integer values are transmitted in network byte order (big-endian).

## Server to Client

Before returning the processed files, the server sends:

```text
4 bytes
Number of output files
```

For each output file, the server then sends:

```text
4 bytes
Filename length
      |
      v
Filename
      |
      v
8 bytes
File size
      |
      v
Processed CZI data
```

This common protocol allows the same Linux server to communicate with each client implementation.

---

# Installation

## Linux Server

The processing server requires the Python environment used by μPiX together with the CZI-processing dependencies used by the project.

Main packages include:

```text
numpy
tifffile
pylibCZIrw
aicspylibczi
```

The μPiX project must also be installed or accessible from the server environment.

## Python Clients

The standalone and time-lapse simulation clients require Python.

The time-lapse simulation client additionally requires:

```text
pylibCZIrw
```

The standalone local client uses Python standard-library modules only.

## ZEISS ZEN Client

The ZEN client must be run from the ZEISS ZEN Blue macro environment.

---

# Running the Project

## 1. Start the Linux server

```bash
python server/mupix_tcp_server.py
```

The server must remain active while clients are being used.

## 2. Choose a client

### Time-lapse simulation

```bash
python clients/timelapse_simulation_client.py
```

### Standalone Windows client

```bash
python clients/local_tcp_client.py
```

### ZEISS ZEN Blue client

Open:

```text
clients/zen_blue_client.py
```

inside the ZEN Blue macro editor and run the macro from ZEN.

---

# Generic Configuration

The repository does not contain workstation-specific IP addresses or personal file-system paths.

Before running a component, replace the generic placeholders with values for your own environment.

### Linux server

```python
MUPIX_DIR = "/path/to/mupix"
SAVE_DIR = "/path/to/input_data"
RESULT_DIR = "/path/to/results"

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 5000
```

### Python clients

```python
SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000
```

Example local paths:

```python
BASE_INPUT_DIR = r"C:\path\to\input"
SAVE_DIR = r"C:\path\to\results"
```

### ZEISS ZEN Blue client

```python
SAVE_PATH = r"C:\path\to\temporary"
SAVE_DIR = r"C:\path\to\results"

SERVER_IP = "SERVER_IP_ADDRESS"
SERVER_PORT = 5000
```

---

# Configuration Notes

Several paths and network addresses in the source code are specific to the development workstations used during the project.

Before using the repository on another computer, update:

- server IP addresses;
- local input/output directories;
- μPiX installation path;
- μPiX experiment directories;
- ZEN experiment name;
- any workstation-specific paths.

For a public GitHub repository, machine-specific paths should preferably be replaced by configuration variables or environment variables.

---

# Project Objective

The objective of this project is to integrate AI-based microscopy image denoising into a microscopy acquisition workflow through TCP/IP communication.

The architecture separates image acquisition from GPU processing:

```text
Microscopy Acquisition
        |
        v
TCP/IP Communication
        |
        v
Linux GPU Processing
        |
        v
μPiX Denoising
        |
        v
CZI Reconstruction
        |
        v
Result Visualization
```

This design allows a single GPU processing workstation to serve different client environments, including ZEISS ZEN Blue, a standalone local computer, and a simulated time-lapse acquisition workflow.

---

# Project Components

| Component | File | Environment | Purpose |
|---|---|---|---|
| Linux GPU Server | `mupix_tcp_server.py` | Linux + GPU | μPiX processing and CZI reconstruction |
| Time-Lapse Simulation Client | `timelapse_simulation_client.py` | Python | Batch and live time-lapse simulation |
| Standalone Local Client | `local_tcp_client.py` | Windows / Python | Send CZI files without ZEN |
| ZEISS ZEN Client | `zen_blue_client.py` | ZEN Blue / IronPython | Acquire, send, receive, and display directly in ZEN |

---

