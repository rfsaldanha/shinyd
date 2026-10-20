import sys

sys.path[:0] = ['/usr/lib/shinyd-control/python', '/usr/lib/shinyd-control/backend']
from deployment.serve import main

main()
