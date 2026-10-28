"""Pulumi program entrypoint. The program itself lives in kluster.main."""

import pulumi

from kluster.main import main

pulumi.run(main)
