# Shake&Tune: 3D printer analysis tools
#
# Copyright (C) 2024 Félix Boisselier <felix@fboisselier.fr> (Frix_x on Discord)
# Licensed under the GNU General Public License v3.0 (GPL-3.0)
#
# File: remote_process.py
# Description: Handles remote graph processing for constrained platforms (e.g. Creality K1C).
#              Uploads the collected .stdata file to a remote Docker server, receives back a ZIP
#              of generated PNG graphs, and extracts them into the local results directory.

import io
import json
import os
import zipfile
from pathlib import Path
from typing import List
from urllib.error import URLError
from urllib.request import Request, urlopen

from .helpers.console_output import ConsoleOutput
from .shaketune_config import ShakeTuneConfig


# Multipart form-data encoding without external dependencies
def _encode_multipart(fields: dict, files: dict, boundary: str) -> bytes:
    """Encode fields and files as multipart/form-data bytes."""
    body = io.BytesIO()
    sep = f'--{boundary}'.encode()
    crlf = b'\r\n'

    for name, value in fields.items():
        body.write(sep + crlf)
        body.write(f'Content-Disposition: form-data; name="{name}"'.encode() + crlf)
        body.write(crlf)
        body.write(value.encode() if isinstance(value, str) else value)
        body.write(crlf)

    for name, (filename, data) in files.items():
        body.write(sep + crlf)
        body.write(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode() + crlf)
        body.write(b'Content-Type: application/octet-stream' + crlf)
        body.write(crlf)
        body.write(data)
        body.write(crlf)

    body.write(f'--{boundary}--'.encode() + crlf)
    return body.getvalue()


def run_remote_process(
    st_config: ShakeTuneConfig,
    graph_type: str,
    filelist: List[Path],
    timeout: float,
    configure_kwargs: dict = None,
) -> None:
    """
    Upload the .stdata file to the remote processing server, receive back a ZIP of PNG graphs,
    and extract them into the correct results subfolder. Falls back gracefully on any network error.
    """
    if not filelist:
        ConsoleOutput.print('Remote processing error: no data files to send.')
        return

    stdata_file = filelist[0]
    if not stdata_file.exists():
        ConsoleOutput.print(f'Remote processing error: data file {stdata_file} does not exist.')
        return

    url = st_config.remote_processing_url.rstrip('/')
    process_url = f'{url}/process'

    # Build config payload to send to server
    config_payload = json.dumps({
        'graph_type': graph_type,
        'max_freq': st_config.max_freq,
        'dpi': st_config.dpi,
        'keep_n_results': st_config.keep_n_results,
        'configure_kwargs': configure_kwargs or {},
    })

    # Read the raw .stdata bytes (no decompression needed — sent as-is)
    try:
        with open(stdata_file, 'rb') as f:
            stdata_bytes = f.read()
    except OSError as e:
        ConsoleOutput.print(f'Remote processing error: could not read data file: {e}')
        return

    # Encode as multipart/form-data
    boundary = 'ShakeTuneBoundary7a8b9c'
    body = _encode_multipart(
        fields={'config': config_payload},
        files={'file': (stdata_file.name, stdata_bytes)},
        boundary=boundary,
    )

    headers = {
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Content-Length': str(len(body)),
    }
    if st_config.remote_api_key:
        headers['Authorization'] = f'Bearer {st_config.remote_api_key}'

    ConsoleOutput.print(f'Sending data to remote processing server at {process_url}...')

    try:
        req = Request(process_url, data=body, headers=headers, method='POST')
        with urlopen(req, timeout=int(timeout)) as response:
            if response.status != 200:
                ConsoleOutput.print(f'Remote processing error: server returned HTTP {response.status}')
                return
            zip_bytes = response.read()
    except URLError as e:
        ConsoleOutput.print(
            f'Remote processing error: could not reach server at {process_url}: {e}\n'
            'Check that your remote_processing_url is correct and the server is running.\n'
            f'Raw data preserved at: {stdata_file}'
        )
        return
    except Exception as e:
        ConsoleOutput.print(f'Remote processing error: unexpected error during upload: {e}')
        return

    # Determine the output folder for this graph type and extract PNGs
    output_folder = st_config.get_results_folder(graph_type)
    output_folder.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            png_names = [name for name in zf.namelist() if name.lower().endswith('.png')]
            if not png_names:
                ConsoleOutput.print('Remote processing error: server returned ZIP with no PNG files.')
                return
            for name in png_names:
                dest = output_folder / Path(name).name
                with zf.open(name) as src, open(dest, 'wb') as dst:
                    dst.write(src.read())
    except zipfile.BadZipFile as e:
        ConsoleOutput.print(f'Remote processing error: server returned an invalid ZIP file: {e}')
        return
    except OSError as e:
        ConsoleOutput.print(f'Remote processing error: could not write graph files: {e}')
        return

    ConsoleOutput.print(f'{graph_type} graphs created successfully (via remote processing)!')

    # Clean up raw data file unless the user wants to keep it
    if not st_config.keep_raw_data:
        try:
            stdata_file.unlink()
        except OSError:
            pass

    # Prune old results, keeping only the configured number
    _clean_old_files(output_folder, st_config.keep_n_results)
    ConsoleOutput.print(
        f'Cleaned up the output folder (only the last {st_config.keep_n_results} results were kept)!'
    )


def _clean_old_files(folder: Path, keep_n: int) -> None:
    """Remove oldest PNG files from folder, keeping only the most recent keep_n."""
    if keep_n <= 0:
        return
    png_files = sorted(folder.glob('*.png'), key=os.path.getmtime)
    for old_file in png_files[:-keep_n] if len(png_files) > keep_n else []:
        try:
            old_file.unlink()
        except OSError:
            pass
