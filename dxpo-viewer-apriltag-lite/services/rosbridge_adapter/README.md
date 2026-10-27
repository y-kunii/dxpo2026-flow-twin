# rosbridge adapter

Owns deployment of the real ROS 2 Jazzy rosbridge WebSocket service.  It does
not change the application topic contract and does not publish to `/tf` or
`/tf_static`.  Docker ownership remains in `docker/Dockerfile.rosbridge` and
`infra/compose/ros.yaml`.
