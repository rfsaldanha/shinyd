import sys

sys.path[:0] = ['/usr/lib/shinyd-control/python', '/usr/lib/shinyd-control/backend']
from deployment.backup import main

main()
