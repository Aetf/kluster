'''Physical layer of the cluster.

This module sets up VMs, basic networking, OSs etc.
'''

from . import aws

def setup():
    aws.setup()
