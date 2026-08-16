"""Pulumi program entrypoint. The program itself lives in kluster.main."""

import pulumi

import kluster.main

pulumi.run(kluster.main.main)
