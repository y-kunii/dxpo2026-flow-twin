# GodotでAprilTagの位置同期を実装する手順

この手順は、カメラ画面が送信した箱の位置・姿勢を、Godot 4の3Dシーンで表示するためのものです。まず背景なしの箱で受信・変換を確認し、その後に3DGS背景と実カメラを組み合わせます。本書に試作と実機確認の手順をまとめ、詳細な通信・座標契約は[位置同期仕様書](apriltag-sync-spec.md)に記載しています。

サンプルは次の条件に限定した検証用です。

| 項目 | 条件 |
| --- | --- |
| 受信側 | Godot 4 / GDScript。サンプルの合成データ試験はGodot 4.6.1と後述のGodotGS検証版で実施 |
| Scene Package | `20260817_DXPO-fixed-rig-spzv4-495a985dfdf1` |
| 固定Tagのframe | `apriltag_tag25h9_0` のみ |
| 箱ID | `box_01` など。最大64件、未割り当てIDも既定寸法で自動生成可能 |
| 座標 | 1 Godot単位 = 1 m。Godotワールドと対象Packageのワールドを一致させる |
| 通信 | 外部のROS 2 rosbridgeにWebSocketで接続。topicは `/box/relative_transform` |

本リポジトリに完成済みのGodotプロジェクトやGodot用SPZローダーはありません。実カメラや相手側のGodotプロジェクト、SPZ背景での連携は未検証です。下記のコードはWeb Viewerのすべての機能を再現するものではありません。

## 1. 準備するもの

1. Godot 4のプロジェクト。既存プロジェクトを使用できます。
2. [Scene Packageのmanifest](../web_viewer/app/public/scene_package/runtime/20260817_DXPO-fixed-rig-spzv4-495a985dfdf1/scene_manifest.json) と [固定Tag map](../web_viewer/app/public/scene_package/runtime/20260817_DXPO-fixed-rig-spzv4-495a985dfdf1/annotations/fixed_tag_map.json)。サンプルの定数はこの2ファイルの対象Packageの値です。
3. Web ViewerとAprilTagカメラ確認画面、および両者とGodotから到達できる外部ROS 2 rosbridge。アプリ自体のURLは資料どおり `http://localhost:8080/` です。rosbridgeはアプリに同梱されません。
4. 実機試験にはC922n、固定Tag 0、箱Tag 20、実物の箱。寸法・取付・カメラ校正は直後の手順で準備します。

同じrosbridge・topicでは送信元を1つだけにし、Godot側と送信側が同じScene Packageを選んでいることを事前に確認します。現行の通信メッセージに送信元やPackageの識別子はなく、自動照合できません。共有rosbridgeを使用すると他の購読者にも姿勢が届きます。

### 1.1 タグとカメラの準備

1. [AprilRoboticsの画像](https://github.com/AprilRobotics/apriltag-imgs)から `tag25h9` のID 0とID 20を用意します。familyを間違えないでください。印刷した黒白境界の検出四隅の辺長を実測します。このPackageの固定Tagは **0.1 m** として登録され、箱Tagも同じ辺長を使います。紙の白い余白は測定に含めません。画面表示したタグを使う場合も実際の表示寸法を測ります。
2. 固定Tag 0を動かない平面に置き、箱Tag 20は箱の上面中央に貼ります。箱の前後を決め、正立したタグ画像の**上辺を箱の後ろ方向**へ向けます。箱の軸は右 `+X`、上 `+Y`、前 `+Z` です。面内で90°回した貼付は現行送信側では補正できません。
3. 箱の幅・高さ・奥行を実測します。初期値は `0.5 / 0.3 / 0.4 m`。実物が異なれば、同じブラウザー・オリジンの多機能Viewerで箱の寸法と貼付面を変更し、Godot側のモデル寸法も合わせます。
4. C922nの映像で両タグが同時に見えるように設置します。ピント、照明、白い余白、遮蔽と反射を確認します。初期処理幅は960 px。カメラ画面の水平画角60°は簡易動作確認用の概算であり、精度評価には次の校正を使います。対角画角78°を水平画角として入力しないでください。

固定Tag 0を撮影現場の登録位置と異なる場所に置いても、Tag 0に対する箱Tagの相対移動は試験できます。ただし、既存3DGS背景と実設備の重なりは確認できません。背景との一致が必要なら、現場の固定Tag位置・向きを登録したScene Packageを別途用意します。

### 1.2 C922nの校正JSONを作る

精度を調べる場合は、C922nの撮影解像度・画角・ズーム・フォーカスを本番と揃え、平らなチェスボードを位置・距離・傾きを変えて10枚以上撮影します。画像全体に渡るようにし、ぼけや反射を避けます。写真はローカルに保存し、外部に送る必要はありません。交点の数はマス目の数ではなく、**内側の交点数**です。[OpenCVのカメラ校正手順](https://docs.opencv.org/4.x/dc/dbb/tutorial_py_calibration.html)も参照できます。

次のコードを作業PCに `calibrate_camera.py` として保存します。Pythonと `opencv-python`、`numpy` が必要です。これはサンプル校正ツールであり、同梱された画像や既製の校正値はありません。

```python
import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import math
import cv2
import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("images", help="チェスボード画像のフォルダー")
parser.add_argument("--cols", type=int, required=True, help="内側の交点の横数")
parser.add_argument("--rows", type=int, required=True, help="内側の交点の縦数")
parser.add_argument("--square-mm", type=float, required=True, help="一マスの実測寸法 mm")
parser.add_argument("--out", default="camera_calibration.json")
args = parser.parse_args()
if args.cols < 2 or args.rows < 2 or not math.isfinite(args.square_mm) or args.square_mm <= 0:
    parser.error("交点数と一マスの寸法は正しく指定してください")
paths = sorted(p for p in Path(args.images).iterdir()
               if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
pattern = (args.cols, args.rows)
obj = np.zeros((args.cols * args.rows, 3), np.float32)
obj[:, :2] = np.mgrid[0:args.cols, 0:args.rows].T.reshape(-1, 2) * (args.square_mm / 1000.0)
object_points, image_points = [], []
image_size = None
for path in paths:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        print("読み飛ばし:", path.name)
        continue
    h, w = img.shape[:2]
    if image_size is None:
        image_size = (w, h)
    if image_size != (w, h):
        raise SystemExit("画像解像度が混在しています。撮影条件を揃えてください。")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCorners(gray, pattern)
    if not found:
        print("交点を検出できないため除外:", path.name)
        continue
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    refined = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    object_points.append(obj.copy())
    image_points.append(refined)
if len(object_points) < 10:
    raise SystemExit(f"有効な画像が{len(object_points)}枚です。異なる姿勢の画像を10枚以上用意してください。")
rms, matrix, dist, _, _ = cv2.calibrateCamera(object_points, image_points, image_size, None, None)
if not math.isfinite(rms) or not np.all(np.isfinite(matrix)) or not np.all(np.isfinite(dist)):
    raise SystemExit("校正値が有限ではありません。撮影条件を見直してください。")
result = {
    "schema_version": "spatial_camera_calibration_v1",
    "image": {"width": image_size[0], "height": image_size[1]},
    "intrinsics": {"fx": float(matrix[0, 0]), "fy": float(matrix[1, 1]),
                   "cx": float(matrix[0, 2]), "cy": float(matrix[1, 2])},
    "distortion": {"model": "opencv_plumb_bob", "coefficients": dist.reshape(-1)[:5].tolist()},
    "camera": {"model": "C922n"},
    "calibrated_at": datetime.now(timezone.utc).isoformat(),
}
Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"校正JSON: {args.out} / 有効画像: {len(object_points)} / RMS再投影誤差: {rms:.4f} px")
print("RMSが小さいだけで実機の位置精度は保証されません。撮影範囲・条件と実測で確認してください。")
```

例えば横9・縦6の内側交点、一マス実測25 mmのボードを使う場合、画像フォルダーのパスを置き換えて実行します。数値は例なので実物に合わせてください。

```powershell
python -m pip install opencv-python numpy
python calibrate_camera.py C:\path\to\calibration_images --cols 9 --rows 6 --square-mm 25 --out camera_calibration.json
```

結果のJSONをカメラ画面の内部パラメータで「校正JSON（歪み補正あり・推奨）」を選んで読み込みます。再投影誤差RMSは記録しますが、その値だけで実機精度の合否を決めることはできません。撮影解像度やフォーカス等を変えた場合は再校正を検討し、実物の既知距離・向きとの比較も行います。元画像や校正JSONには意図せず人物・環境情報が含まれる場合があるため、共有前に内容を確認します。

## 2. Godotに箱を用意する

1. Godotで3Dシーンを作り、ルートに `Node3D` を置きます。
2. ルートの子に `Node3D` を作って `Box01` と名付け、その子に `MeshInstance3D` を追加します。Meshに `BoxMesh` を指定し、Sizeを `(0.5, 0.3, 0.4)` にします。順序は幅・高さ・奥行です。
3. ルートの子に `Node` を追加して `RosReceiver` と名付けます。次節のスクリプトを `res://receiver.gd` に保存し、`RosReceiver` にアタッチします。
4. インスペクターの `Box 01` に `Box01` を割り当てます。`Box 02`～`Box 04` は未割り当てでも構いません。ノードを用意したIDはそれぞれ実物と同じ寸法にします。未割り当てIDは `Auto Create Boxes` が有効な場合、幅0.5 m・高さ0.3 m・奥行0.4 mの箱が生成されます。実寸が異なる場合は表示モデルを別途調整します。
5. シーンに `Camera3D` と必要な照明を追加します。受信後に箱は固定Tag付近のワールド座標へ移るため、カメラをその位置へ向けます。箱の初期表示は受信まで非表示です。

箱の原点は中心、ローカル軸は幅 `+X`、高さ `+Y`、前 `+Z` です。送信側ですでに箱Tagから箱中心へ変換するため、Godot側で同じ補正を重ねません。モデル固有の位置・向きの補正が必要なら、同期対象の `Node3D` の子にモデルを置き、子側で調整します。

## 3. 受信スクリプトを作る

次の全体を `res://receiver.gd` に保存します。アプリ間の型や値は仕様書と合わせていますが、対象Packageの値を固定した例です。異なるPackageでは第7節の対応が必要です。

```gdscript
extends Node

# 本例は対象Scene Packageの固定Tag 0に限定する。
@export var rosbridge_url := "ws://127.0.0.1:9090"
@export var auto_connect := true
@export var auto_create_boxes := true
@export var box_01: Node3D
@export var box_02: Node3D
@export var box_03: Node3D
@export var box_04: Node3D

const TOPIC := "/box/relative_transform"
const FIXED_FRAME := "apriltag_tag25h9_0"
const TIMEOUT_MS := 500
const MAX_JUMP_M := 0.5
const MAX_JUMP_DEG := 45.0
const MAX_BOXES := 64
const T_VIEWER_ROS := Transform3D(
	Basis(Quaternion(-0.5, 0.5, 0.5, 0.5)), Vector3.ZERO)
const T_WORLD_FIXED := Transform3D(
	Basis(Quaternion(-0.004814982222489291, 0.9884368636224934,
		0.0059825152894984355, -0.1514384102172778)),
	Vector3(-1.9498016576367234, -0.059395876925607716, 3.836025736474979))

var socket := WebSocketPeer.new()
var subscribed := false
var connection_pending := false
var boxes: Dictionary = {}
var runtime_boxes: Dictionary = {}
var box_id_pattern := RegEx.new()
# 箱ごとの最後に採用した姿勢・送信元時刻・受信時刻。
var states: Dictionary = {}

func _ready() -> void:
	box_id_pattern.compile("^box_0*[1-9][0-9]*$")
	boxes = {"box_01": box_01, "box_02": box_02, "box_03": box_03, "box_04": box_04}
	_reset_tracking()
	if auto_connect:
		reconnect_bridge()

func reconnect_bridge() -> void:
	socket.close()
	socket = WebSocketPeer.new()
	subscribed = false
	_reset_tracking()
	var err := socket.connect_to_url(rosbridge_url)
	connection_pending = err == OK
	if err != OK:
		push_error("rosbridge connection failed: %s" % err)

func _process(_delta: float) -> void:
	socket.poll()
	var status := socket.get_ready_state()
	if status == WebSocketPeer.STATE_OPEN:
		connection_pending = false
		if not subscribed:
			var req := {"op": "subscribe", "topic": TOPIC,
				"type": "geometry_msgs/msg/TransformStamped"}
			if socket.send_text(JSON.stringify(req)) != OK:
				return
			subscribed = true
		while socket.get_available_packet_count() > 0:
			var packet := socket.get_packet().get_string_from_utf8()
			var envelope: Variant = JSON.parse_string(packet)
			handle_envelope(envelope, Time.get_ticks_msec())
	elif status == WebSocketPeer.STATE_CLOSED:
		if subscribed or connection_pending:
			push_warning("rosbridge disconnected; call reconnect_bridge() to retry")
			_reset_tracking()
		subscribed = false
		connection_pending = false
	update_freshness(Time.get_ticks_msec())

func _reset_tracking() -> void:
	states.clear()
	for id in runtime_boxes:
		var generated: Node3D = runtime_boxes[id]
		boxes.erase(id)
		if is_instance_valid(generated):
			generated.visible = false
			generated.queue_free()
	runtime_boxes.clear()
	for node in boxes.values():
		if is_instance_valid(node):
			node.visible = false

func is_number(value: Variant) -> bool:
	return (typeof(value) == TYPE_INT or typeof(value) == TYPE_FLOAT) and is_finite(float(value))

func valid_numbers(data: Variant, keys: Array) -> bool:
	if typeof(data) != TYPE_DICTIONARY:
		return false
	for key in keys:
		if not data.has(key) or not is_number(data[key]):
			return false
	return true

# 不正な入力は一件だけ破棄し、最後に採用した状態を変更しない。
func handle_envelope(envelope: Variant, received_ms: int) -> bool:
	if typeof(envelope) != TYPE_DICTIONARY or envelope.get("op") != "publish" or envelope.get("topic") != TOPIC:
		return false
	var msg: Variant = envelope.get("msg")
	if typeof(msg) != TYPE_DICTIONARY:
		return false
	var header: Variant = msg.get("header")
	var transform: Variant = msg.get("transform")
	var id: Variant = msg.get("child_frame_id")
	if typeof(header) != TYPE_DICTIONARY or typeof(transform) != TYPE_DICTIONARY or typeof(id) != TYPE_STRING:
		return false
	if header.get("frame_id") != FIXED_FRAME or id.length() > 64 or box_id_pattern.search(id) == null:
		return false
	if not states.has(id) and states.size() >= MAX_BOXES:
		return false
	if not is_instance_valid(boxes.get(id)) and not auto_create_boxes:
		return false
	var stamp: Variant = header.get("stamp")
	if typeof(stamp) != TYPE_DICTIONARY:
		return false
	# ROS 2形式と、Web Viewerが受け付けるROS 1形式の別名に対応。
	var sec: Variant = stamp.get("sec", stamp.get("secs"))
	var nsec: Variant = stamp.get("nanosec", stamp.get("nsecs"))
	if not is_number(sec) or not is_number(nsec):
		return false
	if sec < 0 or nsec < 0 or nsec >= 1000000000 or floor(sec) != sec or floor(nsec) != nsec:
		return false
	# Web Viewerと同じ浮動小数点ミリ秒で箱ごとに比較する。
	var stamp_ms: float = float(sec) * 1000.0 + float(nsec) / 1000000.0
	if not is_finite(stamp_ms):
		return false
	var tr: Variant = transform.get("translation")
	var rot: Variant = transform.get("rotation")
	if not valid_numbers(tr, ["x", "y", "z"]) or not valid_numbers(rot, ["x", "y", "z", "w"]):
		return false
	# JSONの64-bit数値で検査してからQuaternionに変換する。
	var qx: float = rot.x
	var qy: float = rot.y
	var qz: float = rot.z
	var qw: float = rot.w
	var norm := sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
	if not is_finite(norm) or norm <= 1e-12 or abs(norm - 1.0) > 1e-6:
		return false
	var ros_pose := Transform3D(Basis(Quaternion(qx, qy, qz, qw)),
		Vector3(tr.x, tr.y, tr.z))
	if not ros_pose.origin.is_finite():
		return false
	var world_pose: Transform3D = T_WORLD_FIXED * T_VIEWER_ROS * ros_pose * T_VIEWER_ROS.affine_inverse()
	if not world_pose.origin.is_finite():
		return false
	if states.has(id):
		var old: Dictionary = states[id]
		if stamp_ms <= old.stamp_ms:
			return false
		var old_pose: Transform3D = old.pose
		if old_pose.origin.distance_to(world_pose.origin) > MAX_JUMP_M:
			return false
		var qa := old_pose.basis.get_rotation_quaternion().normalized()
		var qb := world_pose.basis.get_rotation_quaternion().normalized()
		var dot := clampf(abs(qa.dot(qb)), 0.0, 1.0)
		if rad_to_deg(2.0 * acos(dot)) > MAX_JUMP_DEG:
			return false
	if not is_instance_valid(boxes.get(id)):
		var generated := Node3D.new()
		generated.name = id
		var mesh := MeshInstance3D.new()
		var cube := BoxMesh.new()
		cube.size = Vector3(0.5, 0.3, 0.4)
		mesh.mesh = cube
		generated.add_child(mesh)
		add_child(generated)
		boxes[id] = generated
		runtime_boxes[id] = generated
	states[id] = {"stamp_ms": stamp_ms, "received_ms": received_ms,
		"pose": world_pose, "tracking": "tracking"}
	var node: Node3D = boxes[id]
	node.global_transform = world_pose
	node.visible = true
	return true

func update_freshness(now_ms: int) -> void:
	for id in states:
		var state: Dictionary = states[id]
		var elapsed: int = now_ms - state.received_ms
		if elapsed > TIMEOUT_MS:
			state.tracking = "lost"
			boxes[id].visible = false
		elif elapsed > TIMEOUT_MS * 0.75 and state.tracking == "tracking":
			state.tracking = "stale"
```

処理の順序は、WebSocket接続・購読、JSONの検証、箱ごとの送信時刻の比較、座標変換、急変判定、姿勢の採用、受信時刻による非表示です。`global_transform` に代入することで、親ノードのローカル座標に直接入れる間違いを避けています。

この例は受信姿勢の更新が375 ms超途絶えると `states[id].tracking` を `stale`、500 ms超で `lost` にして箱を非表示にします。判定はGodotのフレーム更新ごとに行うため、実際の切り替わりにはその分の遅れがあります。各箱の `states` は接続し直したときに消去します。接続が切れた場合の**自動再接続は実装していません**。再試行するときは `RosReceiver.reconnect_bridge()` を呼び出すか、シーンを再実行してください。

## 4. カメラなしで合成データを試す

外部rosbridgeへの送信をせず、まず受信処理の変換と拒否を検証します。以下のスクリプトを**Godotプロジェクト側**の `res://test_receiver.gd` に保存してください。手順3の `res://receiver.gd` が必要です。

```gdscript
extends SceneTree

func sample(sec: int, p: Array, q: Array, id := "box_01") -> Dictionary:
	return {"op":"publish", "topic":"/box/relative_transform", "msg":{
		"header":{"frame_id":"apriltag_tag25h9_0", "stamp":{"sec":sec,"nanosec":0}},
		"child_frame_id":id, "transform":{
			"translation":{"x":p[0],"y":p[1],"z":p[2]},
			"rotation":{"x":q[0],"y":q[1],"z":q[2],"w":q[3]}}}}

func _initialize() -> void:
	var receiver = load("res://receiver.gd").new()
	var box1 := Node3D.new()
	var box2 := Node3D.new()
	root.add_child(box1)
	root.add_child(box2)
	receiver.auto_connect = false
	receiver.box_01 = box1
	receiver.box_02 = box2
	root.add_child(receiver)
	await process_frame
	var inputs = [sample(1,[0,0,0],[0,0,0,1]), sample(1,[1,0,0],[0,0,0,1]), sample(1,[0.25,-0.2,0.1],[0,0,0.7071067812,0.7071067812])]
	var expected = [Vector3(-1.94980166,-0.05939588,3.83602574), Vector3(-1.65036943,-0.06976421,4.79008697), Vector3(-2.06653156,0.03573413,4.13573295)]
	var expected_q = [Quaternion(-0.00481498,0.98843686,0.00598252,-0.15143841), Quaternion(-0.00481498,0.98843686,0.00598252,-0.15143841), Quaternion(-0.00763498,0.59184728,0.00082557,-0.80601354)]
	for i in range(3):
		receiver._reset_tracking()
		assert(receiver.handle_envelope(inputs[i], 100))
		assert(box1.global_position.distance_to(expected[i]) < 0.00001)
		var actual_q := box1.global_transform.basis.get_rotation_quaternion().normalized()
		assert(abs(actual_q.dot(expected_q[i].normalized())) > 0.999999)
		assert(box1.visible)
	receiver._reset_tracking()
	assert(receiver.handle_envelope(inputs[0], 100))
	assert(not receiver.handle_envelope(inputs[0], 200))
	assert(receiver.handle_envelope(sample(1,[0,0,0],[0,0,0,1],"box_02"), 100))
	assert(not receiver.handle_envelope(sample(2,[1,0,0],[0,0,0,1]), 200))
	assert(not receiver.handle_envelope(sample(2,[0,0,0],[0,0,0.7071067812,0.7071067812]), 200))
	assert(not receiver.handle_envelope(sample(2,[0,0,0],[0,0,0,0]), 200))
	assert(not receiver.handle_envelope(sample(2,[0,0,0],[0,0,0,2]), 200))
	receiver.update_freshness(476)
	assert(receiver.states["box_01"].tracking == "stale" and box1.visible)
	receiver.update_freshness(601)
	assert(receiver.states["box_01"].tracking == "lost" and not box1.visible)
	assert(not receiver.handle_envelope(sample(2,[1,0,0],[0,0,0,1]), 602))
	receiver._reset_tracking()
	assert(receiver.handle_envelope(sample(2,[1,0,0],[0,0,0,1]), 603))
	receiver._reset_tracking()
	assert(not receiver.handle_envelope(sample(1,[0,0,0],[0,0,0,1],"box_0"), 100))
	assert(receiver.handle_envelope(sample(1,[0,0,0],[0,0,0,1],"box_05"), 100))
	assert(receiver.runtime_boxes.has("box_05"))
	for i in range(1, 65):
		var id := "box_%d" % i
		if id != "box_05" and receiver.states.size() < 64:
			assert(receiver.handle_envelope(sample(1,[0,0,0],[0,0,0,1],id), 100))
	assert(receiver.states.size() == 64)
	assert(not receiver.handle_envelope(sample(1,[0,0,0],[0,0,0,1],"box_100"), 100))
	receiver._reset_tracking()
	assert(receiver.runtime_boxes.is_empty())
	print("PASS: Godot receiver: A/B/C, duplicate, boxes, invalid quaternion, jump, stale, lost, reset")
	quit()
```

例えばWindowsのPowerShellから、Godotの実行ファイルとプロジェクトのパスを自分の環境に置き換えて実行します。

```powershell
& "C:\path\to\Godot.exe" --headless --path "C:\path\to\godot_project" --script res://test_receiver.gd
```

`PASS: Godot receiver: ...` と表示されれば、3つの変換例、箱ごとの時刻、重複・急変・不正な回転の拒否、追跡喪失、リセットがこのサンプル内で確認できています。各変換例は直前の状態をリセットして独立に実行します。`assert` が出た場合は完了ではありません。これは**内部関数に合成データを渡す試験**で、ネットワーク経由の接続や実カメラを検証するものではありません。

## 5. rosbridgeと接続する

### 5.1 準備と起動例

既存のROS 2 rosbridgeがある場合は、管理者に接続先、使用中の送信元、ROS 2・rosbridge・QoSのバージョンとネットワーク範囲を確認してから使用します。既存のサービスやポートが使用中なら勝手に停止・変更しません。

新たに用意する場合の例はUbuntu 24.04 + ROS 2 Jazzyです。ROS 2がセットアップ済みで、パッケージ提供元が設定されている環境で次を実行します。導入コマンドはその環境の管理者が実行してください。

```bash
sudo apt install ros-jazzy-rosbridge-suite
source /opt/ros/jazzy/setup.bash
ros2 pkg prefix rosbridge_server
ss -ltn 'sport = :9090'
```

9090番が空いていることと、そのPC上のみから使う条件を確認してから、別ターミナルではなく同じターミナルで次を実行します。

```bash
ros2 launch rosbridge_server rosbridge_websocket_launch.xml address:=127.0.0.1 port:=9090
```

この例の `127.0.0.1` は起動したOS自身のみです。別PCから接続する場合は、ネットワーク担当者が接続可能なアドレス、通信範囲、必要な暗号化・認証を決めます。WSLとWindows間では転送方式にも依存するため、Godotを実行する側から接続を確認します。上記のROS 2/rosbridgeは一例で、隔離試験ではJazzyとrosbridge 2.7.1を使用しました。詳細は[公式の起動定義](https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/rosbridge_server/launch/rosbridge_websocket_launch.xml)と[プロトコル仕様](https://github.com/RobotWebTools/rosbridge_suite/blob/ros2/ROSBRIDGE_PROTOCOL.md)を確認してください。

### 5.2 Godotからの接続

1. 接続先のrosbridgeがROS 2の `geometry_msgs/msg/TransformStamped` に対応していることを確認します。送信元と受信元で同じrosbridgeを使用します。共有環境では運用担当者と送信範囲を確認します。
2. Godotの `RosReceiver` の `Rosbridge Url` を設定します。`ws://127.0.0.1:9090` は**Godotを実行するPC自身**の例です。別PCで動くrosbridgeには、そのPCから到達可能なURLを設定します。実際のアドレスは環境に従います。
3. `Auto Connect` を有効にしてGodotシーンを実行します。サンプルは接続後、次の購読要求を送ります。

```json
{"op":"subscribe","topic":"/box/relative_transform","type":"geometry_msgs/msg/TransformStamped"}
```

4. 送信側は接続後に型を指定して `advertise` し、姿勢を `publish` します。通信例とQoSの注意点は[仕様書の第6節](apriltag-sync-spec.md)を参照してください。Godot側は送信側とは別のWebSocket接続です。
5. Godotの出力で接続エラーを確認します。接続できても姿勢が届くまでは箱は表示されません。再接続時には再購読と前回状態のクリアが必要です。

この例はQoSを指定せず、採用するrosbridgeの既定設定に依存します。バージョン固定、明示的なQoS、複数送信元の識別、取得時刻からの鮮度判定は未実装・未決です。

## 6. 実カメラで確認する

1. 固定Tag 0と箱Tag 20を同じカメラ画像に入れます。箱Tagは[仕様書の上面取付方向](apriltag-sync-spec.md)に合わせ、実際の箱の寸法・Tag辺長を設定します。
2. `http://localhost:8080/` の「AprilTagカメラ確認」でC922nとrosbridgeを接続し、`tag25h9`、固定ID 0、箱ID 20 → `box_01` を選びます。複数の送信画面を同時に動かさないでください。
3. 同じブラウザーの別タブで多機能Viewerを開き、動的追跡を同じrosbridgeに接続します。Web Viewerと送信画面の箱設定は同じブラウザー・同じオリジンで共有されます。Godotの箱の寸法は別途合わせます。
4. 送信画面で両タグの同時検出と送信を確認し、GodotとWeb Viewerで `box_01` が表示されることを確認します。小さくゆっくり移動・回転させ、両者の方向と相対的な移動が一致することを見ます。
5. タグを隠し、更新が途絶えてから約500 ms後に箱が消えることを確認します。大きく移動させた後に戻らない場合、各受信画面で接続し直してから初回姿勢を目視確認します。自動再捕捉はありません。

背景の実設備と位置を比較するには、固定TagをScene Package作成時の実位置・向きに置く必要があります。異なる場所では相対的な移動は試せても、背景と正しく重なるとは限りません。

### 6.1 元の現場以外での相対移動試験

1. 固定Tag 0を動かさず、箱の中央位置をテープ等で印付けします。両タグが同じ画像に映る位置で、箱を静止させてGodotとWeb Viewerの姿勢を記録します。
2. 箱の向きを変えずに水平に既知距離だけゆっくり移動し、元の位置と移動後の位置について、実測値と両画面の箱中心間の距離を比較します。例えば0.1 mは手順例であり、合否閾値ではありません。急変として拒否されない速度で移動します。別の方向や角度でも繰り返します。
3. 箱を元に戻し、上から見た回転方向が両画面で一致するか確認します。メジャー等で測った変位・姿勢、校正条件、各画面の値、見失いや拒否を記録します。
4. 位置差の統計を取る場合は、基準の位置と各表示位置を同じ座標に揃えてユークリッド距離を計算します。角度は基準姿勢との回転差の最小角、更新頻度は受信側が採用した件数÷計測時間を使います。遅延は同一時計で実物の動きから表示までを撮影する等、測定区間と時計誤差を明記します。

この試験で確認できるのは検出と相対的な追従です。実設備との3DGS背景の重なりや絶対位置の正しさは確認できません。許容される位置・角度誤差、遅延、更新頻度、測定回数は**未決**です。関係者が数値を決めるまで、実機受け入れの合否は判定しません。

### 6.2 今回の確認記録（2026-10-27）

| 確認項目 | 結果と範囲 |
| --- | --- |
| C922n入力 | Windows上のC922nから1280×720の1フレームを取得し、稼働中アプリの検出APIに渡せた。画像は試験処理中のみ扱った |
| 実タグ検出 | 上記画像では `tag25h9` を検出できなかった。タグ準備が難しいため、この結果は検出・位置同期の成功を意味しない |
| カメラ校正 | 今回のカメラ入力試験は水平画角60°・タグ辺長0.1 mの仮定を使い、実測校正は行っていない |
| rosbridgeとGodot | ROS 2 Jazzy・rosbridge 2.7.1とGodot 4.6.1を隔離環境で接続し、合成された2姿勢の受信・変換、不正回転の拒否とタイムアウトを確認。実カメラからの送信ではない |
| 背景 | 第7.1節のとおり実SPZのheadlessインポートは確認。画面描画と背景・実物の位置合わせは未確認 |
| 実機の精度・遅延 | 実測なし。要求値も未決 |

## 7. 背景・別Packageへの適用

### 7.1 SPZの取得と取り込み例

このPackageはGitHubの `add/dxpo-viewer-apriltag-lite` ブランチにあります。Godot開発用PCですでに該当ブランチをクローンしている場合、リポジトリのルートで次を実行してGit LFSの実データを取得します。

```bash
git lfs install
git lfs pull
git lfs ls-files
```

対象は `dxpo-viewer-apriltag-lite/web_viewer/app/public/scene_package/runtime/20260817_DXPO-fixed-rig-spzv4-495a985dfdf1/splats/20260817_DXPO.spz`、実データのサイズは **108,267,464 bytes**、SHA-256は `5137b4369ac155e3632e5392379f7973c398295310b9406d401fbf3c6c3cd4e1` です。数百バイトのテキストならLFSポインターであり、実データではありません。Package IDの `spzv4` は名前の一部で、ファイルを調べた結果、実際のSPZ形式は**バージョン3**、Gaussianは500万点、SH次数2でした。インポーターがSPZ v3に対応するか確認してください。

標準Godotに同梱のSPZローダーはないため、選択肢の一例として[GodotGS](https://github.com/klausi3D/godotGS)を挙げます。これは通常のGodotに追加するプラグインではなく、**Godot 4.5をベースとした別のエディター**です。Alpha版のため、既存のGodotプロジェクトや元データを直接変更せず、コピーで評価し、導入前にライセンス、OS・GPU・配布形態、使用中Godotバージョンとの互換性を確認してください。[取り込み手順](https://klausi3d.github.io/godotGS/latest/features/artist_pipeline/)に従い次を行います。

1. GodotGSの対応エディターを入手し、コピーした評価用Godotプロジェクトを開きます。Git LFSで取得したSPZのコピーをGodotプロジェクトの `res://` 内へ置き、インポート終了を待ちます。
2. インポート後、FileSystemのSPZを3Dビューにドラッグするか、`GaussianSplatNode3D` を追加して `splat_asset` にインポート済みアセットを割り当てます。大量データのためディスク・メモリ・GPU性能が必要です。
3. [world_alignment.json](../web_viewer/app/public/scene_package/runtime/20260817_DXPO-fixed-rig-spzv4-495a985dfdf1/config/world_alignment.json)の `generated_to_world` は、このPackageでは **scale 0.9065493454633762、回転 `[0,0,0,1]`、並進 `[0,0,0]`** です。Web ViewerではSparkによるSPZデコード後、背景にこの変換を一度だけ適用しています。Godotのインポーターが返す座標をSparkと比較してから同じワールドに位置合わせします。異なるデコーダーが同じ軸変換をするとは限らず、scaleや軸変換を重複適用しないでください。箱の受信座標にはこの背景用scaleを掛けません。
4. 背景の複数の特徴点・距離・上下左右をWeb ViewerとGodotで比較し、固定Tag mapの位置・向きに重なることを確認します。1点だけでは回転や尺度の誤りを検出できません。異なる場合、インポーターの座標変換を記録し、背景側の補正を調整して再比較します。

検証結果: `nightly-20261027` のLinux GodotGS（表示バージョン `4.5.rc.custom_build.e16996baa`）を、ネットワーク・ホストポートなしの隔離環境で実行し、対象SPZの500万点を `GaussianSplatAsset` としてインポートできました。同じ版で第4節の受信コードの合成試験も通過しました。これは**headlessでのデータ取り込みと受信処理**の確認で、GPUによる実画面表示、Sparkと同じ背景向き、Godot 4.6の既存プロジェクトでの利用、Windows版、実機との重なりは未検証です。Linux版の試験中は描画デバイス・フォント未使用に関する警告が出ています。

### 7.2 別Packageと複数箱

- 別Packageを扱う実装では、manifestの `coordinate_system.T_viewer_ros`、`dynamic_overlay.freshness.timeout_ms`、固定Tag mapの各frameを読み込んで使用します。別Packageの動的物体設定がある場合の急変閾値は[仕様書の第7.3.1節](apriltag-sync-spec.md)を参照してください。このサンプルはJSON自動読込を実装していません。
- JSONファイルをGodotへコピーして `FileAccess` で読む場合、書き出し時にも含める設定が必要です。Godotの[FileAccessの説明](https://docs.godotengine.org/en/4.4/classes/class_fileaccess.html)に従い、対象ファイルのインポート設定や非リソース書き出しフィルターを確認します。
- 受信側は最大64件を扱えますが、送信側が使う `tag25h9` の箱IDは20～34の15種類です。複数箱を同時に追跡する場合はTag IDと箱IDを一対一で割り当てます。受信箱IDは文字列として扱うため `box_1` と `box_01` を混在させないでください。自動生成されたモデルの個別寸法設定やエラー表示は別途実装が必要です。

## 8. うまくいかない場合

| 症状 | 確認すること |
| --- | --- |
| 接続できない | Godotから見たURL、rosbridgeの稼働状態とネットワーク、`ws` / `wss` の違いを確認。切断後は再接続が必要 |
| 接続できたが箱が出ない | 両タグの同時検出、送信中表示、topic・frame名・箱ID、Godotの箱ノード割り当て、カメラの向きを確認 |
| 箱の向きが90°ずれる | タグ上辺と箱軸が取付契約どおりか確認。現行送信側に面内回転の補正設定はない |
| 背景と箱がずれる | 固定Tagの実位置・向き、Packageの一致、背景の原点・尺度・軸、モデル中心を確認 |
| タグが再検出されても箱が戻らない | 旧姿勢から0.5 m超または45°超の急変が拒否され得る。受信側を再接続して確認 |
| 値が頻繁に止まる | タグの見切れ・照明・画質・校正、同一時刻・逆行、急変、QoSや通信状態を確認 |

実機の位置誤差・角度誤差・遅延・更新頻度の合否値は未決です。測定条件と受け入れ方法は[仕様書の第8節](apriltag-sync-spec.md)を参照し、関係者で基準を決めてから実機を評価してください。
