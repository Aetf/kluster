# kluster-py

Pulumi python code to build my k8s cluster using Talos on GCP.

Project managed by [uv](https://github.com/astral-sh/uv)

## GCP
* n2d-standard-2 spot
  * 30G boot disk

$32.67/month

## AWS

region: us-west-2

* t2g.small not-spot 3year upfront
  * 20G EBS
* t2g.medium not-spot 3year upfront
  * 100G EBS

$827.82 (3y) + $12.8/month

https://calculator.aws/#/estimate?id=add73a7a40b1599c4d3164cd1da9f0aa98f62d86

Need to move S3 to us-west-2 region.
* Stop juicefs
* Stop hath (for how long? need to check)


### networking using aws vpc

__Not possible to use any BGP or ARP based load balancer on Cloud__

Since all nodes has public IP, use HostPort for traefik

traefik deployment on all control nodes, and host port for all entry points

use tcp entrypoint and tcp router for other cluster services like hath

use multiple AAAA records for load balancing among notes

still kube router can be used for in-cluster cluster IP and service IP routing, as well as BGP with DMSE...

1 public ipv6 only subnet with internet gateway, one with each control node
  aws DHCP options use nat64.net DNS64 servers
  aws security group to allow kube-api and talos port on each node
  aws security group to allow inbound wireguard udp port on each node

  aws lb for kube-api targeting all control nodes

1 public ipv6 only subnet with egress-only internet gateway, with workers
  aws DHCP options use nat64.net DNS64 servers
  aws security group to allow all inbound from same security group
  aws security group to allow all inbound from control node security group

1 lan (non aws) worker node on lan

  kube-router need to advertise cluster ip on lan host (node label annotation)

kube-router as cni, service proxy (disable kube-proxy in talos) and load balancer
  install use own manifest, the github provided one unnecessarily includes kubeconfig, while it only needs the api server address, which is localhost:6443 thanks to KubePrism
  need to set cluster ip (ip assigned to clusterIP service) range

  need to advertise cluster ip on lan host
