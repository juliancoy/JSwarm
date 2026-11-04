import argparse
import json
import math
import sys
from .hardware import Hardware


def main():
    parser = argparse.ArgumentParser(description='ramtop — Linux memory performance monitor')
    parser.add_argument('--diagnose', action='store_true', help='print CPU, PMUs, perf list and access errors as JSON')
    parser.add_argument('--interval', type=float, default=1, help='refresh seconds (0.25–10)')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or not .25 <= args.interval <= 10:
        parser.error('--interval must be between 0.25 and 10 seconds')
    if sys.platform != 'linux':
        parser.error('ramtop requires Linux')
    hardware = Hardware()
    try:
        if args.diagnose:
            print(json.dumps(hardware.report(), indent=2))
        else:
            from .app import Ramtop
            Ramtop(hardware, args.interval).run()
    finally:
        hardware.close()


if __name__ == '__main__':
    main()
