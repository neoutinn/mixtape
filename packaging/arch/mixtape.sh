#!/bin/sh
export PYTHONPATH="/usr/lib/mixtape-ipod${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m mixtape "$@"
