FROM ros:humble

WORKDIR /workspace/digital_twin_ws

# 必要パッケージ
RUN apt-get update && apt-get install -y \
    python3-colcon-common-extensions \
    python3-rosdep \
    ros-humble-rosbridge-server \
 && rm -rf /var/lib/apt/lists/*

# エントリポイント
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["start"]
