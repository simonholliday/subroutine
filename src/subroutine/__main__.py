"""``python -m subroutine`` — the same command as the ``subroutine`` script.

Worth having for two reasons beyond tidiness. A virtualenv whose ``bin`` is not on the path
still has a working command, which is the situation in a container more often than not; and a
test that needs a *separate process* — one serving over a real socket — can start one without
knowing where the console script was installed.
"""

import subroutine.cli.main

if __name__ == "__main__":
	subroutine.cli.main.main()
