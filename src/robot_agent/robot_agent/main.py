import threading

import rclpy
from rclpy.executors import MultiThreadedExecutor
import uvicorn

from .agent_node import AgentNode
from .app import create_app


def main():
    rclpy.init()
    node = AgentNode()

    # Run rclpy executor in a background thread
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    ros_thread = threading.Thread(target=executor.spin, daemon=True)
    ros_thread.start()

    # Create FastAPI app
    app = create_app(node)

    node.get_logger().info('Starting FastAPI server on http://0.0.0.0:8080')

    try:
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info('Shutting down...')
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
