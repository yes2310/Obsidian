#!/usr/bin/env python3
"""
Korean Whisper - Maximum Speed (Quality Preserved)
Batched Inference + CPU 워커 10개 + VAD 필터 + beam_size 5
"""

import argparse
import multiprocessing as mp
from pathlib import Path
import json
import time
import subprocess
import os
import math
import shutil
from concurrent.futures import ThreadPoolExecutor

SUPPORTED_INPUT_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a"}


def get_gpu_info():
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free", 
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True
    )
    gpus = []
    for line in result.stdout.strip().split('\n'):
        if line:
            parts = [p.strip() for p in line.split(',')]
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "total_mb": int(parts[2]),
                "free_mb": int(parts[3])
            })
    return gpus


def get_video_duration(video_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def recommend_batch_size(free_mb: int, model_name: str, compute_type: str) -> int:
    """VRAM 여유를 기준으로 보수적으로 배치 크기 추천."""
    if free_mb >= 18000:
        batch = 32
    elif free_mb >= 14000:
        batch = 24
    elif free_mb >= 11000:
        batch = 20
    elif free_mb >= 9000:
        batch = 16
    elif free_mb >= 7000:
        batch = 12
    elif free_mb >= 5000:
        batch = 8
    elif free_mb >= 3500:
        batch = 6
    else:
        batch = 4

    if compute_type != "float16":
        batch = int(batch * 1.2)

    # 작은 모델일수록 여유 있게 사용
    if model_name in {"tiny", "base"}:
        batch = int(batch * 1.5)
    elif model_name in {"small", "medium"}:
        batch = int(batch * 1.25)

    return max(1, min(batch, 32))


def parse_gpu_ids(value: str) -> list[int]:
    gpu_ids = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            gpu_ids.append(int(raw))
        except ValueError as exc:
            raise ValueError(f"GPU ID는 숫자와 쉼표로만 입력하세요: {value}") from exc
    if not gpu_ids:
        raise ValueError("GPU ID 목록이 비어 있습니다.")
    return gpu_ids


def select_gpus(gpus: list[dict], gpu_ids_value: str | None, gpu_count: int | None) -> list[dict]:
    if not gpus:
        raise RuntimeError("nvidia-smi에서 사용 가능한 GPU를 찾지 못했습니다.")

    idx_to_gpu = {g["index"]: g for g in gpus}
    if gpu_ids_value:
        wanted = parse_gpu_ids(gpu_ids_value)
    else:
        requested_count = gpu_count if gpu_count and gpu_count > 0 else len(gpus)
        if requested_count > len(gpus):
            print(f"⚠️ 요청한 GPU {requested_count}개 중 사용 가능 GPU는 {len(gpus)}개입니다. 가능한 GPU만 사용합니다.")
        wanted = [g["index"] for g in gpus[:max(1, min(requested_count, len(gpus)))]]

    missing = [i for i in wanted if i not in idx_to_gpu]
    if missing:
        available = ", ".join(str(g["index"]) for g in gpus)
        raise ValueError(f"요청한 GPU 인덱스가 존재하지 않습니다: {missing} (사용 가능: {available})")

    return [idx_to_gpu[i] for i in wanted]


def _numeric_select(files, input_dir: Path):
    print(f"\n📂 {input_dir} 영상 목록")
    for i, f in enumerate(files, 1):
        print(f"  {i}. {f.name}")
    while True:
        choice = input("번호 선택 (Enter 취소): ").strip()
        if choice == "":
            raise KeyboardInterrupt("선택 취소")
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(files):
                return files[idx - 1]
        print("유효한 번호를 입력하세요.")


def _curses_select(files, input_dir: Path):
    import curses

    def _picker(stdscr):
        curses.curs_set(0)
        idx = 0
        while True:
            stdscr.clear()
            title = f"Input: {input_dir} (↑/↓ 선택, Enter 확정, q 취소)"
            stdscr.addnstr(0, 0, title, curses.COLS - 1)
            for i, f in enumerate(files):
                prefix = "> " if i == idx else "  "
                line = f"{prefix}{f.name}"
                if i == idx:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addnstr(i + 2, 0, line, curses.COLS - 1)
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addnstr(i + 2, 0, line, curses.COLS - 1)
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord('k')):
                idx = (idx - 1) % len(files)
            elif key in (curses.KEY_DOWN, ord('j')):
                idx = (idx + 1) % len(files)
            elif key in (curses.KEY_ENTER, ord('\n'), ord('\r')):
                return files[idx]
            elif key in (27, ord('q')):
                raise KeyboardInterrupt("선택 취소")

    return curses.wrapper(_picker)


def pick_input_file(input_dir: Path) -> Path:
    input_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_INPUT_EXTS],
        key=lambda p: p.name.lower()
    )
    if not files:
        raise FileNotFoundError(f"{input_dir} 에서 지원하는 영상/오디오 파일을 찾지 못했습니다.")

    try:
        return _curses_select(files, input_dir)
    except Exception as e:
        print(f"⚠️ 터미널 화살표 선택을 사용할 수 없어 숫자 선택으로 전환합니다 ({e})")
        return _numeric_select(files, input_dir)


def extract_audio_segment(args: tuple) -> str:
    video_path, start, end, output_path = args
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
        "-t", str(end - start), "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1", "-threads", "4", "-f", "wav", output_path
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def transcribe_segment(audio_path: str, gpu_id: int, segment_idx: int, 
                       start_time: float, segment_duration: float,
                       model_name: str, compute_type: str, batch_size: int,
                       progress_queue: mp.Queue):
    """배치 추론 + VAD + CPU 워커 최적화"""
    
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    
    import torch
    
    # cuDNN 최적화
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.enabled = True
    
    torch.cuda.init()
    torch.cuda.empty_cache()
    
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    
    progress_queue.put({"gpu": gpu_id, "status": "loading"})
    
    # 모델 로드 (CPU 워커 10개로 최대화)
    model = WhisperModel(
        model_name,
        device="cuda",
        device_index=0,
        compute_type=compute_type,
        num_workers=10,  # 10개로 증가
        cpu_threads=8,
    )
    
    # 배치 추론 파이프라인 (핵심 최적화)
    batched_model = BatchedInferencePipeline(model=model)
    
    progress_queue.put({"gpu": gpu_id, "status": "processing", "progress": 0})
    
    # 품질 유지 + 속도 최적화 설정
    segments_iter, info = batched_model.transcribe(
        audio_path,
        language="ko",
        task="transcribe",
        beam_size=5,        # 품질 유지
        best_of=5,          # 품질 유지
        temperature=0,
        condition_on_previous_text=True,
        vad_filter=True,    # VAD 활성화 (무음 스킵)
        vad_parameters={
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 200,
        },
        word_timestamps=False,
        without_timestamps=False,
        batch_size=batch_size,
    )
    
    adjusted_segments = []
    full_text_parts = []
    last_update = 0
    
    for seg in segments_iter:
        adjusted_segments.append({
            "start": seg.start + start_time,
            "end": seg.end + start_time,
            "text": seg.text
        })
        full_text_parts.append(seg.text)
        
        # 2초마다 진행률 업데이트
        if seg.end - last_update >= 2:
            pct = min(int((seg.end / segment_duration) * 100), 99)
            progress_queue.put({"gpu": gpu_id, "status": "processing", "progress": pct})
            last_update = seg.end
    
    progress_queue.put({"gpu": gpu_id, "status": "done", "progress": 100})
    
    del batched_model
    del model
    torch.cuda.empty_cache()
    
    return {
        "success": True,
        "segment_idx": segment_idx,
        "segments": adjusted_segments,
        "text": " ".join(full_text_parts),
        "gpu_id": gpu_id
    }


def worker(args):
    """워커 함수"""
    (audio_path, gpu_id, segment_idx, start_time, segment_duration, 
     model_name, compute_type, batch_size, progress_queue) = args
    try:
        return transcribe_segment(
            audio_path, gpu_id, segment_idx, 
            start_time, segment_duration,
            model_name, compute_type, batch_size, progress_queue
        )
    except Exception as e:
        import traceback
        progress_queue.put({"gpu": gpu_id, "status": "error", "error": str(e)})
        traceback.print_exc()
        return {
            "success": False,
            "segment_idx": segment_idx,
            "error": str(e)
        }


def progress_monitor(progress_queue: mp.Queue, gpu_ids: list[int], stop_event: mp.Event):
    """tqdm 진행률 모니터"""
    from tqdm import tqdm
    
    bars = {}
    for position, gpu_id in enumerate(gpu_ids):
        bars[gpu_id] = tqdm(
            total=100,
            desc=f"GPU {gpu_id}",
            position=position,
            leave=True,
            bar_format="{desc}: {bar:30} {percentage:3.0f}% | {postfix}"
        )
        bars[gpu_id].set_postfix_str("대기중")
    
    while not stop_event.is_set():
        try:
            msg = progress_queue.get(timeout=0.3)
            gpu = msg["gpu"]
            status = msg["status"]
            if gpu not in bars:
                continue
            
            if status == "loading":
                bars[gpu].set_postfix_str("모델 로딩...")
            elif status == "processing":
                progress = msg.get("progress", 0)
                bars[gpu].n = progress
                bars[gpu].set_postfix_str("처리중")
                bars[gpu].refresh()
            elif status == "done":
                bars[gpu].n = 100
                bars[gpu].set_postfix_str("✅ 완료")
                bars[gpu].refresh()
            elif status == "error":
                bars[gpu].set_postfix_str(f"❌ {msg.get('error', '')[:20]}")
        except:
            pass
    
    for bar in bars.values():
        bar.close()
    print()


def format_timestamp_srt(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def main():
    parser = argparse.ArgumentParser(description="한국어 Whisper (최고 속도 + 품질 유지)")
    parser.add_argument("video", nargs="?", help="영상 파일 (미지정 시 input 폴더에서 선택)")
    parser.add_argument("-i", "--input-dir", default="input", help="입력 영상 폴더 (기본: input)")
    parser.add_argument("-O", "--output-root", default="output", help="출력 기본 폴더 (기본: output/<강의명>/)")
    parser.add_argument("-o", "--output", help="출력 경로(파일)")
    parser.add_argument("-b", "--batch-size", type=int, help="Whisper batch size (기본: VRAM에 따라 자동)")
    parser.add_argument("-m", "--model", default="large-v3",
                        choices=["tiny", "base", "small", "medium", 
                                 "large-v1", "large-v2", "large-v3"])
    parser.add_argument("-g", "--gpus", type=int, default=None)
    parser.add_argument("--gpu-ids", default=os.environ.get("WHISPER_GPU_IDS"),
                        help="사용할 GPU 인덱스 목록. 예: 0 또는 0,1 (기본: 사용 가능한 GPU 자동 선택)")
    parser.add_argument("-c", "--compute", default="float16",
                        choices=["float16", "int8_float16", "int8"])
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    input_dir = Path(args.input_dir)
    if not input_dir.is_absolute():
        input_dir = (base_dir / input_dir).resolve()
    input_dir.mkdir(parents=True, exist_ok=True)

    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = (base_dir / output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    if args.video:
        candidate = Path(args.video)
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if not candidate.exists():
            alt = (input_dir / args.video).resolve()
            if alt.exists():
                candidate = alt
        if not candidate.exists():
            raise FileNotFoundError(f"영상 파일을 찾을 수 없습니다: {args.video}")
        video_path = candidate
    else:
        video_path = pick_input_file(input_dir)
        video_path = video_path.resolve()

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
    else:
        video_stem = video_path.stem
        output_dir = output_root / video_stem
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{video_stem}.txt"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    video_path = video_path.resolve()
    output_path = output_path.resolve()
    video_path_str = str(video_path)

    print(f"\n{'='*55}")
    print(f"🎬 영상: {video_path_str}")
    print(f"📂 입력 폴더: {input_dir}")
    print(f"📁 결과 폴더: {output_path.parent}")
    
    gpus = get_gpu_info()
    gpus_in_use = select_gpus(gpus, args.gpu_ids, args.gpus)
    num_gpus = len(gpus_in_use)
    gpu_ids = [gpu["index"] for gpu in gpus_in_use]

    batch_sizes = []
    for gpu in gpus_in_use:
        if args.batch_size and args.batch_size > 0:
            batch_sizes.append(args.batch_size)
        else:
            batch_sizes.append(recommend_batch_size(gpu["free_mb"], args.model, args.compute))
    
    print(f"💻 GPU {num_gpus}개 | 모델: {args.model} ({args.compute})")
    print(f"⚡ 최적화: Batched Inference + VAD + CPU워커 10개")
    for i, gpu in enumerate(gpus_in_use):
        free_gb = gpu['free_mb'] / 1024
        print(f"   GPU {gpu['index']}: {gpu['name']} (free {free_gb:.1f}GB) | batch {batch_sizes[i]}")
    
    duration = get_video_duration(video_path_str)
    print(f"📏 길이: {format_time(duration)} ({duration:.0f}초)")
    
    # 세그먼트 분할
    seg_dur = math.ceil(duration / num_gpus)
    segments_info = []
    for i, gpu in enumerate(gpus_in_use):
        seg_start = i * seg_dur
        seg_end = min((i + 1) * seg_dur, duration)
        if seg_start < duration:
            segments_info.append((seg_start, seg_end, gpu["index"], batch_sizes[i]))
    
    # RAM 디스크 사용
    temp_dir = f"/dev/shm/whisper_{os.getpid()}"
    os.makedirs(temp_dir, exist_ok=True)
    
    print(f"\n🎵 오디오 추출중...")
    extract_args = [
        (video_path_str, s, e, f"{temp_dir}/seg_{g}.wav")
        for s, e, g, _ in segments_info
    ]
    with ThreadPoolExecutor(max_workers=num_gpus) as ex:
        temp_files = list(ex.map(extract_audio_segment, extract_args))
    
    print(f"📦 세그먼트 분할:")
    for s, e, g, b in segments_info:
        print(f"   GPU {g}: {format_time(s)} ~ {format_time(e)} ({format_time(e-s)}) | batch {b}")
    
    print(f"\n🚀 GPU 병렬 처리 시작\n")
    
    # 진행률 모니터 설정
    manager = mp.Manager()
    progress_queue = manager.Queue()
    stop_event = manager.Event()
    
    monitor = mp.Process(target=progress_monitor, args=(progress_queue, gpu_ids, stop_event))
    monitor.start()
    
    start_time = time.time()
    
    # 워커 인자 준비
    worker_args = []
    for i, (seg_start, seg_end, gpu_id, batch_size) in enumerate(segments_info):
        worker_args.append((
            temp_files[i], gpu_id, i, seg_start, seg_end - seg_start,
            args.model, args.compute, batch_size, progress_queue
        ))
    
    # 병렬 실행
    with mp.Pool(num_gpus, maxtasksperchild=1) as pool:
        results = pool.map(worker, worker_args)
    
    # 모니터 종료
    stop_event.set()
    time.sleep(0.5)
    monitor.terminate()
    monitor.join()
    
    # 정리
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    elapsed = time.time() - start_time
    
    # 결과 병합
    successful = sorted(
        [r for r in results if r.get("success")], 
        key=lambda x: x["segment_idx"]
    )
    failed = len(results) - len(successful)
    
    if failed > 0:
        print(f"\n⚠️ {failed}개 세그먼트 실패")
    
    all_segments = []
    full_text_parts = []
    for r in successful:
        all_segments.extend(r["segments"])
        full_text_parts.append(r["text"])
    
    full_text = " ".join(full_text_parts)
    
    # 저장
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    
    srt_path = str(Path(output_path).with_suffix(".srt"))
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(all_segments, 1):
            f.write(f"{i}\n{format_timestamp_srt(seg['start'])} --> {format_timestamp_srt(seg['end'])}\n{seg['text'].strip()}\n\n")
    
    json_path = str(Path(output_path).with_suffix(".json"))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "video": video_path_str,
            "model": args.model,
            "compute_type": args.compute,
            "optimizations": ["batched_inference", "vad_filter", "cpu_workers_10", "cudnn_benchmark"],
            "duration": duration,
            "processing_time": elapsed,
            "realtime_speed": round(duration / elapsed, 1),
            "text": full_text,
            "segments": all_segments
        }, f, ensure_ascii=False, indent=2)
    
    # 결과 출력
    speed = duration / elapsed
    print(f"\n{'='*55}")
    print(f"✅ 완료!")
    print(f"")
    print(f"⏱️  처리 시간: {format_time(elapsed)} ({elapsed:.0f}초)")
    print(f"🚀 속도: {speed:.1f}x 실시간")
    print(f"")
    print(f"📄 텍스트: {output_path}")
    print(f"🎬 자막: {srt_path}")
    print(f"📋 JSON: {json_path}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    mp.set_start_method('spawn', force=True)
    main()
