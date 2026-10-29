[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$VideoPath,

    [Parameter(Mandatory)]
    [string]$OutputPath,

    [ValidateRange(0.05,10.0)]
    [double]$FrameIntervalSec = 0.20,

    [switch]$DisablePersonMask,

    [switch]$DisableManhattanAlignment
)

$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'

function Write-Section {
    param([string]$Text)
    Write-Output ''
    Write-Output '================================================================'
    Write-Output $Text
    Write-Output '================================================================'
}

function Invoke-Exe {
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$Name
    )

    Write-Output ("> $Name " + ($Arguments -join ' '))

    $oldPreference=$ErrorActionPreference
    $ErrorActionPreference='Continue'

    & $Exe @Arguments
    $exitCode=$LASTEXITCODE

    $ErrorActionPreference=$oldPreference

    if($exitCode -ne 0){
        throw "$Name failed with exit code $exitCode."
    }
}

function Invoke-Colmap {
    param(
        [string]$ColmapBat,
        [string[]]$Arguments
    )

    $joined=($Arguments | ForEach-Object {
        '"' + $_.Replace('"','""') + '"'
    }) -join ' '

    $command='call "' + $ColmapBat + '" ' + $joined

    Write-Output ("> COLMAP " + ($Arguments -join ' '))

    $oldPreference=$ErrorActionPreference
    $ErrorActionPreference='Continue'

    & cmd.exe /d /c $command
    $exitCode=$LASTEXITCODE

    $ErrorActionPreference=$oldPreference

    if($exitCode -ne 0){
        throw "COLMAP failed with exit code $exitCode."
    }
}

$scriptRoot=Split-Path -Parent $PSCommandPath
$webRoot=Join-Path $scriptRoot 'webapp'
$configPath=Join-Path $webRoot 'tool_config.json'
$renderer=Join-Path $webRoot 'render_pano_rig.py'
$python=Join-Path $webRoot '.venv\Scripts\python.exe'
$personMaskRoot=Join-Path $scriptRoot 'person_mask'
$personMaskPython=Join-Path $personMaskRoot '.venv\Scripts\python.exe'
$personMaskScript=Join-Path $personMaskRoot 'generate_person_colmap_masks.py'

if(!(Test-Path $configPath)){throw "tool_config.json not found: $configPath"}
if(!(Test-Path $renderer)){throw "Renderer not found: $renderer"}
if(!(Test-Path $python)){throw "Web app Python environment not found: $python"}

if(-not $DisablePersonMask){
    if(!(Test-Path $personMaskPython)){
        throw "Person-mask Python environment not found: $personMaskPython"
    }

    if(!(Test-Path $personMaskScript)){
        throw "Person-mask generator not found: $personMaskScript"
    }
}

$config=Get-Content $configPath -Raw | ConvertFrom-Json
$colmap=[string]$config.colmap_bat
$ffmpeg=[string]$config.ffmpeg
$ffprobe=Join-Path (Split-Path $ffmpeg -Parent) 'ffprobe.exe'

if(!(Test-Path $colmap)){throw "COLMAP.bat not found: $colmap"}
if(!(Test-Path $ffmpeg)){throw "ffmpeg.exe not found: $ffmpeg"}
if(!(Test-Path $ffprobe)){throw "ffprobe.exe not found: $ffprobe"}

$VideoPath=(Resolve-Path $VideoPath -ErrorAction Stop).Path
$OutputPath=[System.IO.Path]::GetFullPath($OutputPath)

if(!(Test-Path $VideoPath -PathType Leaf)){
    throw "Video not found: $VideoPath"
}

if(Test-Path $OutputPath){
    $existing=Get-ChildItem $OutputPath -Force -ErrorAction SilentlyContinue
    if($existing){
        throw "Output folder must be new or empty: $OutputPath"
    }
}

New-Item -ItemType Directory -Force -Path $OutputPath | Out-Null

$panoramas=Join-Path $OutputPath 'panoramas'
$images=Join-Path $OutputPath 'images'
$masks=Join-Path $OutputPath 'masks'
$masksPerson=Join-Path $OutputPath 'masks_person'
$masksColmap=Join-Path $OutputPath 'masks_colmap'
$personMaskReport=Join-Path $OutputPath 'person_mask_report.json'
$database=Join-Path $OutputPath 'database.db'
$rigConfig=Join-Path $OutputPath 'rig_config.json'
$manifest=Join-Path $OutputPath 'render_manifest.json'
$sparse=Join-Path $OutputPath 'sparse'
$report=Join-Path $OutputPath 'report.txt'

Write-Section '360 Video to Fixed-Rig COLMAP SfM'
Write-Output "Input video: $VideoPath"
Write-Output "Output path: $OutputPath"
Write-Output "Frame interval: $FrameIntervalSec seconds"
Write-Output "COLMAP: $colmap"

Write-Section '1/7 Video validation'
$probeText=(& $ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of json $VideoPath 2>&1 | Out-String)

try{
    $probe=$probeText | ConvertFrom-Json -ErrorAction Stop
}
catch{
    throw "Could not parse video metadata from ffprobe:`n$probeText"
}

if(-not $probe.streams -or $probe.streams.Count -lt 1){
    throw "No video stream was found by ffprobe."
}

$width=[int]$probe.streams[0].width
$height=[int]$probe.streams[0].height

if($width -le 0 -or $height -le 0){
    throw "Invalid video resolution: ${width}x${height}"
}

if($width -ne ($height * 2)){
    throw "A 2:1 equirectangular 360 video is required. Detected: ${width}x${height}"
}

Write-Output "Equirectangular video: ${width}x${height}"

Write-Section '2/7 Lossless PNG frame extraction'
New-Item -ItemType Directory -Force -Path $panoramas | Out-Null
$fps=(1.0 / $FrameIntervalSec).ToString('0.########',[System.Globalization.CultureInfo]::InvariantCulture)

Invoke-Exe -Exe $ffmpeg -Name 'FFmpeg' -Arguments @(
    '-hide_banner',
    '-loglevel','warning',
    '-y',
    '-i',$VideoPath,
    '-map','0:v:0',
    '-vf',"fps=$fps",
    (Join-Path $panoramas 'frame_%06d.png')
)

$frameCount=@(Get-ChildItem $panoramas -Filter '*.png' -File).Count

if($frameCount -lt 8){
    throw "Too few panorama frames were extracted: $frameCount"
}

Write-Output "Panorama frames: $frameCount"

Write-Section '3/7 12 overlapping virtual cameras and fixed rig'
Invoke-Exe -Exe $python -Name 'Python renderer' -Arguments @(
    $renderer,
    '--input_dir',$panoramas,
    '--image_dir',$images,
    '--mask_dir',$masks,
    '--rig_config',$rigConfig,
    '--manifest',$manifest
)

$render=Get-Content $manifest -Raw | ConvertFrom-Json
$viewCount=[int]$render.virtual_cameras_per_panorama
$expectedImageCount=$frameCount * $viewCount
$actualImageCount=@(Get-ChildItem $images -Recurse -Filter '*.png' -File).Count

if($actualImageCount -ne $expectedImageCount){
    throw "Virtual-view count mismatch. Expected=$expectedImageCount Actual=$actualImageCount"
}

Write-Output "Virtual cameras per panorama: $viewCount"
Write-Output "Virtual images: $actualImageCount"
Write-Output "Virtual image size: $($render.virtual_camera_width)x$($render.virtual_camera_height)"


$colmapMasks=$masks

if($DisablePersonMask){
    Write-Output 'Person masking: disabled. Using virtual-view static masks only.'
}else{
    Write-Section '4/7 Grounding DINO + SAM 2.1 Large person masks'

    $env:HF_HOME=Join-Path $personMaskRoot 'hf_cache'
    $env:HF_HUB_DISABLE_TELEMETRY='1'

    Invoke-Exe -Exe $personMaskPython -Name 'Grounding DINO + SAM 2 person masks' -Arguments @(
        $personMaskScript,
        '--image_root',$images,
        '--static_mask_root',$masks,
        '--person_mask_root',$masksPerson,
        '--output_mask_root',$masksColmap,
        '--report',$personMaskReport,
        '--box_threshold','0.25',
        '--text_threshold','0.20',
        '--nms_threshold','0.50',
        '--margin','12'
    )

    if(!(Test-Path $personMaskReport)){
        throw "Person mask report was not created: $personMaskReport"
    }

    $colmapMasks=$masksColmap
    Write-Output "COLMAP feature masks: $colmapMasks"
    Write-Output "Person mask report: $personMaskReport"
}

Write-Section '5/7 COLMAP GPU feature extraction, rig setup, and matching'
Invoke-Colmap -ColmapBat $colmap -Arguments @(
    'feature_extractor',
    '--database_path',$database,
    '--image_path',$images,
    '--ImageReader.single_camera_per_folder','1',
    '--ImageReader.mask_path',$colmapMasks,
    '--FeatureExtraction.use_gpu','1',
    '--SiftExtraction.max_num_features','8192'
)

Invoke-Colmap -ColmapBat $colmap -Arguments @(
    'rig_configurator',
    '--database_path',$database,
    '--rig_config_path',$rigConfig
)

Invoke-Colmap -ColmapBat $colmap -Arguments @(
    'sequential_matcher',
    '--database_path',$database,
    '--FeatureMatching.use_gpu','1',
    '--FeatureMatching.guided_matching','1',
    '--FeatureMatching.rig_verification','1',
    '--FeatureMatching.skip_image_pairs_in_same_frame','1',
    '--SequentialMatching.overlap','10',
    '--SequentialMatching.loop_detection','1'
)

Write-Section '6/7 COLMAP fixed-rig mapper'
New-Item -ItemType Directory -Force -Path $sparse | Out-Null

Invoke-Colmap -ColmapBat $colmap -Arguments @(
    'mapper',
    '--database_path',$database,
    '--image_path',$images,
    '--output_path',$sparse,
    '--Mapper.ba_refine_sensor_from_rig','0',
    '--Mapper.ba_refine_focal_length','0',
    '--Mapper.ba_refine_principal_point','0',
    '--Mapper.ba_refine_extra_params','0'
)

$model=Get-ChildItem $sparse -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name |
    Select-Object -First 1

if(!$model){
    throw 'COLMAP sparse model was not created.'
}

$analysisFile=Join-Path $OutputPath 'model_analyzer.txt'
$analysisCommand='call "' + $colmap + '" model_analyzer --path "' + $model.FullName + '" > "' + $analysisFile + '" 2>&1'

$oldPreference=$ErrorActionPreference
$ErrorActionPreference='Continue'
& cmd.exe /d /c $analysisCommand
$analysisExit=$LASTEXITCODE
$ErrorActionPreference=$oldPreference

if($analysisExit -ne 0){
    throw "model_analyzer failed with exit code $analysisExit."
}

@(
    '360 Video to Fixed-Rig COLMAP SfM'
    ''
    "Input video: $VideoPath"
    "Output path: $OutputPath"
    "Frame interval: $FrameIntervalSec seconds"
    "Panorama frames: $frameCount"
    "Virtual cameras per panorama: $viewCount"
    "Virtual images: $actualImageCount"
    "Sparse model: $($model.FullName)"
    ''
    'Model analysis:'
    (Get-Content $analysisFile -Raw)
) | Set-Content $report -Encoding UTF8


$alignedModel=$null

Write-Section '7/7 Manhattan-world orientation alignment'

if($DisableManhattanAlignment){
    Write-Output 'Manhattan alignment: disabled. Using original sparse model.'
}else{
    $sourceModel=Get-ChildItem $sparse -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name |
        Select-Object -First 1

    if($null -eq $sourceModel){
        throw "No sparse model was found for Manhattan alignment: $sparse"
    }

    $manhattanRoot=Join-Path $OutputPath 'sparse_manhattan'
    $alignedModel=Join-Path $manhattanRoot '0'

    if(Test-Path $manhattanRoot){
        throw "Manhattan output already exists and will not be overwritten: $manhattanRoot"
    }

    New-Item -ItemType Directory -Force -Path $alignedModel | Out-Null

    Invoke-Colmap -ColmapBat $colmap -Arguments @(
        'model_orientation_aligner',
        '--image_path',$images,
        '--input_path',$sourceModel.FullName,
        '--output_path',$alignedModel,
        '--method','MANHATTAN-WORLD',
        '--max_image_size','1024'
    )

    $requiredAlignedFiles=@(
        (Join-Path $alignedModel 'cameras.bin'),
        (Join-Path $alignedModel 'images.bin'),
        (Join-Path $alignedModel 'points3D.bin')
    )

    $missingAlignedFiles=@(
        $requiredAlignedFiles |
        Where-Object { -not (Test-Path $_) }
    )

    if($missingAlignedFiles.Count -gt 0){
        throw "Manhattan alignment did not create required files:`n$($missingAlignedFiles -join "`n")"
    }

    Write-Output "Manhattan-aligned sparse model: $alignedModel"
}

Write-Section 'Completed'
Write-Output "Sparse model (original): $($model.FullName)"

if($alignedModel){
    Write-Output "Sparse model (Manhattan-aligned): $alignedModel"
}
Write-Output "Report: $report"
Write-Output "Images for downstream 3DGS: $images"
