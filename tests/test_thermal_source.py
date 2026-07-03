"""parse_thermal_source() — the pure source-resolution logic for the thermal
handler (V4L2 device vs screen-share region). No cv2/mss/hardware needed."""
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.dashboard.handlers.thermal_handler import parse_thermal_source


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    check('none disabled', parse_thermal_source('none') == ('none', None))
    check('None disabled', parse_thermal_source(None) == ('none', None))
    check('empty disabled', parse_thermal_source('') == ('none', None))

    check('screen full', parse_thermal_source('screen') == ('screen', None))
    check('screen region',
          parse_thermal_source('screen:100,50,640,480') ==
          ('screen', {'left': 100, 'top': 50, 'width': 640, 'height': 480}))
    check('screen bad region → full',
          parse_thermal_source('screen:foo') == ('screen', None))

    check('/dev/video2 → v4l2 2', parse_thermal_source('/dev/video2') == ('v4l2', 2))
    check('int index', parse_thermal_source(3) == ('v4l2', 3))
    check('digit string', parse_thermal_source('4') == ('v4l2', 4))

    kind, spec = parse_thermal_source('auto')
    check('auto → v4l2 device', kind == 'v4l2' and isinstance(spec, int))

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
