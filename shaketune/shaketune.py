# Shake&Tune: 3D printer analysis tools
#
# Copyright (C) 2024 Félix Boisselier <felix@fboisselier.fr> (Frix_x on Discord)
# Licensed under the GNU General Public License v3.0 (GPL-3.0)
#
# File: shaketune.py
# Description: Main class implementation for Shake&Tune, handling Klipper initialization and
#              loading of the plugin, and the registration of the tuning commands


import importlib
import os
from pathlib import Path
from typing import Callable

from .commands import (
    axes_map_calibration,
    axes_shaper_calibration,
    compare_belts_responses,
    create_vibrations_profile,
    excitate_axis_at_freq,
)
from .helpers.console_output import ConsoleOutput
from .shaketune_config import ShakeTuneConfig
from .shaketune_process import ShakeTuneProcess

# Try to import graph creators. They require matplotlib/numpy which are unavailable on
# platforms like the Creality K1C (MIPS32). On those platforms, remote_processing_url
# must be configured so that graph generation is offloaded to a Docker server.
try:
    from .graph_creators import GraphCreatorFactory
    _LOCAL_PROCESSING_AVAILABLE = True
except ImportError:
    _LOCAL_PROCESSING_AVAILABLE = False


# Positional arg name mappings for each graph type's configure() method.
# Used by GraphCreatorStub to serialize configure args for remote processing.
_CONFIGURE_PARAM_NAMES = {
    'input shaper': ('scv', 'max_smoothing', 'test_params', 'max_scale'),
    'belts comparison': ('kinematics', 'test_params', 'max_scale'),
    'axes map': ('accel', 'current_axes_map'),
    'vibrations profile': ('kinematics', 'accel', 'motors'),
    'static frequency': ('freq', 'duration', 'accel_per_hz'),
}

_SKIP = object()  # sentinel for non-JSON-serializable values


def _to_serializable(value):
    """Convert a configure() arg to a JSON-serializable form, or return _SKIP."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, '_asdict'):
        # namedtuple (e.g. testParams) — convert to a plain list
        return list(value)
    if isinstance(value, (tuple, list)):
        items = [_to_serializable(v) for v in value]
        if any(v is _SKIP for v in items):
            return _SKIP
        return items
    # Complex Python object (e.g. Motor) — cannot serialize, skip
    return _SKIP


class GraphCreatorStub:
    """Lightweight graph creator stub for remote processing mode.

    Does NOT import matplotlib or numpy — fully usable on constrained platforms
    (e.g. Creality K1C / MIPS32) where those libraries are unavailable.

    Provides the same interface as GraphCreator so command functions work unchanged.
    configure() captures serializable args to send to the remote Docker server.
    """

    def __init__(self, graph_type: str, config: ShakeTuneConfig) -> None:
        self._graph_type = graph_type
        self._config = config
        self._configure_kwargs = {}

    def get_type(self) -> str:
        return self._graph_type

    def get_folder(self):
        folder = self._config.get_results_folder(self._graph_type)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def configure(self, *args, **kwargs) -> None:
        """Capture configure args as a JSON-serializable dict for remote processing."""
        param_names = _CONFIGURE_PARAM_NAMES.get(self._graph_type, ())
        result = {}
        for i, name in enumerate(param_names):
            val = args[i] if i < len(args) else kwargs.get(name)
            serializable = _to_serializable(val)
            if serializable is not _SKIP:
                result[name] = serializable
        # Capture any extra keyword args not covered by the positional mapping
        for k, v in kwargs.items():
            if k not in result:
                serializable = _to_serializable(v)
                if serializable is not _SKIP:
                    result[k] = serializable
        self._configure_kwargs = result

    def define_output_target(self, filepath) -> None:
        pass  # No-op: the remote server determines its own output path

    def clean_old_files(self, keep_results: int = 10) -> None:
        pass  # Handled by remote_process.py after receiving results

    def get_configure_kwargs(self) -> dict:
        return self._configure_kwargs

DEFAULT_FOLDER = '~/printer_data/config/ShakeTune_results'
DEFAULT_NUMBER_OF_RESULTS = 10
DEFAULT_KEEP_RAW_DATA = False
DEFAULT_MAX_FREQ = 200.0
DEFAULT_DPI = 150
DEFAULT_TIMEOUT = 600
DEFAULT_SHOW_MACROS = True
DEFAULT_MEASUREMENTS_CHUNK_SIZE = 2  # Maximum number of measurements to keep in memory at once
ST_COMMANDS = {
    'EXCITATE_AXIS_AT_FREQ': (
        'Maintain a specified excitation frequency for a period of time to diagnose and locate a source of vibrations'
    ),
    'AXES_MAP_CALIBRATION': (
        'Perform a set of movements to measure the orientation of the accelerometer '
        'and help you set the best axes_map configuration for your printer'
    ),
    'COMPARE_BELTS_RESPONSES': (
        'Perform a custom half-axis test to analyze and compare the '
        'frequency profiles of individual belts on CoreXY or CoreXZ printers'
    ),
    'AXES_SHAPER_CALIBRATION': 'Perform standard axis input shaper tests on one or both XY axes to select the best input shaper filter',
    'CREATE_VIBRATIONS_PROFILE': (
        'Run a series of motions to find speed/angle ranges where the printer could be '
        'exposed to VFAs to optimize your slicer speed profiles and TMC driver parameters'
    ),
}


class ShakeTune:
    def __init__(self, config) -> None:
        self._config = config
        self._printer = config.get_printer()
        self._printer.register_event_handler('klippy:connect', self._on_klippy_connect)

        # Check if Shake&Tune is running in DangerKlipper
        self.IN_DANGER = importlib.util.find_spec('extras.danger_options') is not None

        # Register the console print output callback to the corresponding Klipper function
        gcode = self._printer.lookup_object('gcode')
        ConsoleOutput.register_output_callback(gcode.respond_info)

        st_config, timeout, show_macros = self._initialize_config(config)
        self._st_config = st_config
        self.timeout = timeout
        self._show_macros = show_macros

        self._register_commands()

    # Initialize the ShakeTune object and its configuration
    def _initialize_config(self, k_conf) -> None:
        result_folder = k_conf.get('result_folder', default=DEFAULT_FOLDER)
        result_folder_path = Path(result_folder).expanduser() if result_folder else None
        keep_n_results = k_conf.getint('number_of_results_to_keep', default=DEFAULT_NUMBER_OF_RESULTS, minval=0)
        keep_raw_data = k_conf.getboolean('keep_raw_data', default=DEFAULT_KEEP_RAW_DATA)
        max_freq = k_conf.getfloat('max_freq', default=DEFAULT_MAX_FREQ, minval=100.0)
        dpi = k_conf.getint('dpi', default=DEFAULT_DPI, minval=100, maxval=500)
        m_chunk_size = k_conf.getint('measurements_chunk_size', default=DEFAULT_MEASUREMENTS_CHUNK_SIZE, minval=2)
        remote_url = k_conf.get('remote_processing_url', default=None)
        remote_key = k_conf.get('remote_api_key', default=None)
        st_config = ShakeTuneConfig(result_folder_path, keep_n_results, keep_raw_data, m_chunk_size, max_freq, dpi,
                                    remote_processing_url=remote_url, remote_api_key=remote_key)
        timeout = k_conf.getfloat('timeout', DEFAULT_TIMEOUT, above=0.0)
        show_macros = k_conf.getboolean('show_macros_in_webui', default=DEFAULT_SHOW_MACROS)
        return st_config, timeout, show_macros

    # Create the Klipper commands to allow the user to run Shake&Tune's tools
    def _register_commands(self) -> None:
        gcode = self._printer.lookup_object('gcode')
        measurement_commands = [
            ('EXCITATE_AXIS_AT_FREQ', self.cmd_EXCITATE_AXIS_AT_FREQ, ST_COMMANDS['EXCITATE_AXIS_AT_FREQ']),
            ('AXES_MAP_CALIBRATION', self.cmd_AXES_MAP_CALIBRATION, ST_COMMANDS['AXES_MAP_CALIBRATION']),
            ('COMPARE_BELTS_RESPONSES', self.cmd_COMPARE_BELTS_RESPONSES, ST_COMMANDS['COMPARE_BELTS_RESPONSES']),
            ('AXES_SHAPER_CALIBRATION', self.cmd_AXES_SHAPER_CALIBRATION, ST_COMMANDS['AXES_SHAPER_CALIBRATION']),
            ('CREATE_VIBRATIONS_PROFILE', self.cmd_CREATE_VIBRATIONS_PROFILE, ST_COMMANDS['CREATE_VIBRATIONS_PROFILE']),
        ]

        # Register Shake&Tune's measurement commands using the official Klipper API (gcode.register_command)
        # Doing this makes the commands available in Klipper but they are not shown in the web interfaces
        # and are only available by typing the full name in the console (like all the other Klipper commands)
        for name, command, description in measurement_commands:
            gcode.register_command(f'_{name}' if self._show_macros else name, command, desc=description)

        # Then, a hack to inject the macros into Klipper's config system in order to show them in the web
        # interfaces. This is not a good way to do it, but it's the only way to do it for now to get
        # a good user experience while using Shake&Tune (it's indeed easier to just click a macro button)
        if self._show_macros:
            configfile = self._printer.lookup_object('configfile')
            dirname = os.path.dirname(os.path.realpath(__file__))
            filename = os.path.join(dirname, 'dummy_macros.cfg')
            try:
                dummy_macros_cfg = configfile.read_config(filename)
            except Exception as err:
                raise self._config.error(f'Cannot load Shake&Tune dummy macro {filename}') from err

            for gcode_macro in dummy_macros_cfg.get_prefix_sections('gcode_macro '):
                gcode_macro_name = gcode_macro.get_name()

                # Replace the dummy description by the one from ST_COMMANDS (to avoid code duplication and define it in only one place)
                command = gcode_macro_name.split(' ', 1)[1]
                description = ST_COMMANDS.get(command, 'Shake&Tune macro')
                gcode_macro.fileconfig.set(gcode_macro_name, 'description', description)

                # Add the section to the Klipper configuration object with all its options
                if not self._config.fileconfig.has_section(gcode_macro_name.lower()):
                    self._config.fileconfig.add_section(gcode_macro_name.lower())
                for option in gcode_macro.fileconfig.options(gcode_macro_name):
                    value = gcode_macro.fileconfig.get(gcode_macro_name, option)
                    self._config.fileconfig.set(gcode_macro_name.lower(), option, value)
                    # Small trick to ensure the new injected sections are considered valid by Klipper config system
                    self._config.access_tracking[(gcode_macro_name.lower(), option.lower())] = 1

                # Finally, load the section within the printer objects
                self._printer.load_object(self._config, gcode_macro_name.lower())

    def _on_klippy_connect(self) -> None:
        # Check if the resonance_tester object is available in the printer
        # configuration as it is required for Shake&Tune to work properly
        res_tester = self._printer.lookup_object('resonance_tester', None)
        if res_tester is None:
            raise self._config.error(
                'No [resonance_tester] config section found in printer.cfg! Please add one to use Shake&Tune!'
            )

        # Ensure the output folders exist
        for f in self._st_config.get_results_subfolders():
            f.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------------------------
    # ------------------------------------------------------------------------------------------
    # Following are all the Shake&Tune commands that are registered to the Klipper console
    # ------------------------------------------------------------------------------------------
    # ------------------------------------------------------------------------------------------

    def _cmd_helper(self, gcmd, graph_type: str, cmd_function: Callable) -> None:
        ConsoleOutput.print(f'Shake&Tune version: {ShakeTuneConfig.get_git_version()}')
        if self._st_config.remote_processing_url:
            # Remote processing mode: graph generation is offloaded to a Docker server.
            # Use a lightweight stub that doesn't require matplotlib/numpy.
            gcreator = GraphCreatorStub(graph_type, self._st_config)
        elif _LOCAL_PROCESSING_AVAILABLE:
            gcreator = GraphCreatorFactory.create_graph_creator(graph_type, self._st_config)
        else:
            raise gcmd.error(
                'matplotlib/numpy are not installed and remote_processing_url is not configured. '
                'Please either install the required dependencies or set remote_processing_url '
                'in [shaketune] to use a Docker processing server.'
            )
        st_process = ShakeTuneProcess(
            self._st_config,
            self._printer.get_reactor(),
            gcreator,
            self.timeout,
        )
        cmd_function(gcmd, self._config, st_process)

    def cmd_EXCITATE_AXIS_AT_FREQ(self, gcmd) -> None:
        self._cmd_helper(gcmd, 'static frequency', excitate_axis_at_freq)

    def cmd_AXES_MAP_CALIBRATION(self, gcmd) -> None:
        self._cmd_helper(gcmd, 'axes map', axes_map_calibration)

    def cmd_COMPARE_BELTS_RESPONSES(self, gcmd) -> None:
        self._cmd_helper(gcmd, 'belts comparison', compare_belts_responses)

    def cmd_AXES_SHAPER_CALIBRATION(self, gcmd) -> None:
        self._cmd_helper(gcmd, 'input shaper', axes_shaper_calibration)

    def cmd_CREATE_VIBRATIONS_PROFILE(self, gcmd) -> None:
        self._cmd_helper(gcmd, 'vibrations profile', create_vibrations_profile)
