# Shake&Tune: 3D printer analysis tools
#
# Copyright (C) 2024 Félix Boisselier <felix@fboisselier.fr> (Frix_x on Discord)
# Licensed under the GNU General Public License v3.0 (GPL-3.0)
#
# File: docker/server.py
# Description: FastAPI HTTP server that accepts .stdata files from a Creality K1C (or any
#              remote client), generates Shake&Tune graphs using the full numpy/matplotlib
#              stack, and returns the resulting PNG files as a ZIP archive.
#
# Usage:
#   docker compose up                      # via Docker
#   uvicorn docker.server:app --port 8080  # local development

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response

# ---------------------------------------------------------------------------
# Klipper shaper_calibrate setup
# ---------------------------------------------------------------------------
# The server needs Klipper's shaper_calibrate and shaper_defs modules for the
# input shaper and belts graphs. We load them from KLIPPER_DIR if set, otherwise
# we try to import them from the Python path (useful if running inside a Klipper venv).

_KLIPPER_DIR = os.environ.get('KLIPPER_DIR', '')
if _KLIPPER_DIR and Path(_KLIPPER_DIR).is_dir():
    klipper_extras = str(Path(_KLIPPER_DIR) / 'klippy')
    if klipper_extras not in sys.path:
        sys.path.insert(0, klipper_extras)

try:
    import importlib
    sys.modules['shaper_calibrate'] = importlib.import_module('extras.shaper_calibrate')
    sys.modules['shaper_defs'] = importlib.import_module('extras.shaper_defs')
    _SHAPER_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _SHAPER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Shake&Tune imports
# ---------------------------------------------------------------------------
import json

from shaketune.graph_creators import GraphCreatorFactory
from shaketune.helpers.accelerometer import MeasurementsManager
from shaketune.shaketune_config import ShakeTuneConfig

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title='Shake&Tune Remote Processing Server', version='1.0.0')

_API_KEY = os.environ.get('SHAKETUNE_API_KEY', '').strip()


def _check_auth(authorization: Optional[str] = None) -> None:
    """Validate Bearer token if SHAKETUNE_API_KEY is configured."""
    if not _API_KEY:
        return  # Auth disabled — local network use
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Missing Authorization header')
    token = authorization[len('Bearer '):]
    if token != _API_KEY:
        raise HTTPException(status_code=403, detail='Invalid API key')


# ---------------------------------------------------------------------------
# Graph type → CLI name mapping (matches shaketune/shaketune.py ST_COMMANDS)
# ---------------------------------------------------------------------------
_VALID_GRAPH_TYPES = {
    'input shaper',
    'belts comparison',
    'axes map',
    'vibrations profile',
    'static frequency',
}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get('/health')
def health():
    from shaketune.shaketune_config import ShakeTuneConfig
    return {
        'status': 'ok',
        'version': ShakeTuneConfig.get_git_version(),
        'shaper_available': _SHAPER_AVAILABLE,
    }


@app.post('/process')
async def process(
    file: UploadFile = File(...),
    config: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    """
    Accept a .stdata file and a JSON config, generate Shake&Tune graphs, and return
    a ZIP archive of the resulting PNG files.

    Config JSON fields:
        graph_type (str): One of the five Shake&Tune graph types.
        max_freq (float): Maximum frequency for graph rendering.
        dpi (int): Graph DPI.
        keep_n_results (int): Not used server-side (cleanup is done on K1C).
        configure_kwargs (dict): Serialized configure() args from GraphCreatorStub.
    """
    _check_auth(authorization)

    # Parse config JSON
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f'Invalid config JSON: {e}')

    graph_type = cfg.get('graph_type', '')
    if graph_type not in _VALID_GRAPH_TYPES:
        raise HTTPException(status_code=400, detail=f'Unknown graph_type: {graph_type!r}')

    max_freq = float(cfg.get('max_freq', 200.0))
    dpi = int(cfg.get('dpi', 150))
    configure_kwargs = cfg.get('configure_kwargs', {})

    # Warn if input shaper / belts requested but shaper_calibrate not available
    if graph_type in ('input shaper', 'belts comparison') and not _SHAPER_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail=(
                'Klipper shaper_calibrate module not found. '
                'Mount your Klipper directory to /klipper in the container, '
                'or set the KLIPPER_DIR environment variable.'
            ),
        )

    # Read uploaded .stdata bytes
    stdata_bytes = await file.read()
    if not stdata_bytes:
        raise HTTPException(status_code=400, detail='Uploaded file is empty')

    # Work in a temporary directory so parallel requests don't collide
    with tempfile.TemporaryDirectory(prefix='shaketune_') as tmpdir:
        tmpdir = Path(tmpdir)
        stdata_path = tmpdir / (file.filename or 'measurement.stdata')
        stdata_path.write_bytes(stdata_bytes)

        # Set up a ShakeTuneConfig pointing at the temp directory
        st_config = ShakeTuneConfig(
            result_folder=tmpdir,
            keep_n_results=9999,   # server never prunes; K1C handles retention
            keep_raw_data=False,
            chunk_size=2,
            max_freq=max_freq,
            dpi=dpi,
        )

        # Create the appropriate graph creator
        try:
            graph_creator = GraphCreatorFactory.create_graph_creator(graph_type, st_config)
        except NotImplementedError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Re-inflate test_params list back to a tuple if present (namedtuple was serialized as list)
        if 'test_params' in configure_kwargs and isinstance(configure_kwargs['test_params'], list):
            configure_kwargs['test_params'] = tuple(configure_kwargs['test_params'])

        # Configure the graph creator with the parameters from the K1C
        try:
            graph_creator.configure(**configure_kwargs)
        except TypeError as e:
            raise HTTPException(status_code=400, detail=f'Invalid configure parameters: {e}')

        # Define the output target (the base path — graph_creator will append .png)
        output_base = tmpdir / stdata_path.stem
        graph_creator.define_output_target(output_base)

        # Load measurements from the .stdata file
        m_manager = MeasurementsManager(chunk_size=2)
        if stdata_path.suffix == '.stdata':
            m_manager.load_from_stdata(stdata_path)
        elif stdata_path.suffix == '.csv':
            m_manager.load_from_csvs([stdata_path])
        else:
            raise HTTPException(status_code=400, detail='Only .stdata or .csv files are supported')

        if not m_manager.get_measurements():
            raise HTTPException(status_code=422, detail='No measurements found in the uploaded file')

        # Generate the graph(s)
        try:
            graph_creator.create_graph(m_manager)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f'Graph generation failed: {e}')

        # Collect all generated PNG files and return as ZIP
        png_files = list(tmpdir.glob('*.png'))
        if not png_files:
            raise HTTPException(status_code=500, detail='Graph generation produced no PNG files')

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for png in png_files:
                zf.write(png, arcname=png.name)
        zip_bytes = zip_buffer.getvalue()

    return Response(
        content=zip_bytes,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="shaketune_{graph_type.replace(" ", "_")}.zip"'},
    )
