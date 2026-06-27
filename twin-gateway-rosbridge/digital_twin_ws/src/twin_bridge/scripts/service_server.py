#!/usr/bin/env python3
import rclpy
from rclpy.node import Node

#from twin_bridge.srv import TwinCommand
from twin_bridge.msg import SimpleMsg

class CommandService(Node):
    def __init__(self):
        super().__init__('command_service')

        # Service
        #self.srv = self.create_service(TwinCommand, 'twin_command', self.execute_callback)

        # Publisher (SimpleMsg)
        self.publisher_ = self.create_publisher(SimpleMsg, 'simple_topic', 10)
        self.get_logger().info('Advertised topic: /simple_topic (no publishing)')

        # 定期的にPublishするタイマー (1秒ごと)
        #self.timer = self.create_timer(1.0, self.timer_callback)
        #self.counter = 0

    def execute_callback(self, request, response):
        self.get_logger().info(f"Received command: {request.command}")
        response.success = True
        response.message = f"Command {request.command} executed for {request.target_id}"

        # サービス呼び出しに応じてメッセージをPublishすることも可能
        msg = SimpleMsg()
        msg.id = self.counter
        msg.text = f"Service triggered: {request.command}"
        self.publisher_.publish(msg)

        return response

    def timer_callback(self):
        msg = SimpleMsg()
        msg.id = self.counter
        msg.text = f"Heartbeat {self.counter}"
        self.publisher_.publish(msg)
        self.get_logger().info(f'Published: id={msg.id}, text="{msg.text}"')
        self.counter += 1


def main(args=None):
    rclpy.init(args=args)
    node = CommandService()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
