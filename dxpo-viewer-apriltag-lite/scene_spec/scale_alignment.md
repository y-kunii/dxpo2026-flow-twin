# Scale Alignment

このシステムでは、3DGS / Proxy Mesh / カメラプリセット / 人体モデル / 測距結果をすべてメートル単位のワールド座標で扱う。

3DGS生成直後の座標系は、SfM / 3DGS生成処理に依存し、必ずしも実寸スケールではない。
そのため、AprilTag または既知寸法を使って、生成座標を実世界スケールに合わせる。

## 基本方針

- viewer-web では AprilTag 検出を行わない。
- AprilTag検出とスケール推定は generation_pipeline 側で行う。
- viewer-web は scene_package/config/world_alignment.json を読み、必要に応じて座標変換を適用する。
- 距離測定は、world_alignment 適用後のメートル座標で行う。

## 座標系

- 単位は meter とする。
- up axis は Y とする。
- scene_package 内の座標は原則として実世界スケールに正規化済みとする。
- 正規化前の座標系を保持する場合は、world_alignment.json に変換情報を記録する。

## 変換の考え方

生成座標からワールド座標への変換は以下で表す。

world_position = scale * R * generated_position + t

- scale: 実寸スケール係数
- R: 回転
- t: 平行移動
