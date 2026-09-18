"""Fail the container check on exceptions, dependency errors or GTag warnings."""
from pathlib import Path
import re
import sys

lines = Path(sys.argv[1]).read_text().splitlines()
bad = [line for line in lines if (
    re.search(r'\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)|ModuleNotFoundError|ImportError', line)
    or ('WARNING' in line and 'custom_components.gtag_ble_test' in line)
)]
if bad:
    raise SystemExit('HA Container logged unexpected problems:\n' + '\n'.join(bad))
print('HA Container logs: no errors, exceptions or GTag warnings')
