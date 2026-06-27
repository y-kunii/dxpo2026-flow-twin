cmake_minimum_required(VERSION 3.8)
project(twin_bridge)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

# msg/srv をここに列挙
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/SimpleMsg.msg"
  # "srv/YourSrv.srv"  # 追加があれば列挙
)

# Pythonノードは「スクリプト」としてインストール
install(PROGRAMS
  scripts/service_server.py
  DESTINATION lib/${PROJECT_NAME}
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
