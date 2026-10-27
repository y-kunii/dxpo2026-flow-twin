export interface VideoFrameTimestamp {
  epochMs: number;
  mediaTimeS: number;
  presentedFrames: number;
  source: "captureTime" | "requestVideoFrameCallback" | "mediaTimeProjection" | "analysisCycleFallback";
}

export interface VideoFrameClock {
  start(): void;
  stop(): void;
  timestampForCycle(cycleStarted: number): VideoFrameTimestamp;
}

/**
 * Own image-time observation independently from AprilTag detection state.
 * Chromium may pause frame callbacks for a hidden integrated view even while
 * the media timeline advances, so timestampForCycle projects from mediaTime
 * without inventing a fresh timestamp for a repeated frame.
 */
export function createVideoFrameClock(
  video: HTMLVideoElement,
  isActive: () => boolean,
): VideoFrameClock {
  let callbackId: number | undefined;
  let latest: VideoFrameTimestamp | undefined;

  const stop = (): void => {
    if (callbackId !== undefined && typeof video.cancelVideoFrameCallback === "function") {
      video.cancelVideoFrameCallback(callbackId);
    }
    callbackId = undefined;
    latest = undefined;
  };
  const start = (): void => {
    stop();
    if (typeof video.requestVideoFrameCallback !== "function") return;
    const observe = (now: DOMHighResTimeStamp, metadata: VideoFrameCallbackMetadata): void => {
      if (!isActive()) return;
      const captureTime = Number(metadata.captureTime);
      latest = {
        epochMs: performance.timeOrigin + (Number.isFinite(captureTime) ? captureTime : now),
        mediaTimeS: metadata.mediaTime,
        presentedFrames: metadata.presentedFrames,
        source: Number.isFinite(captureTime) ? "captureTime" : "requestVideoFrameCallback",
      };
      callbackId = video.requestVideoFrameCallback(observe);
    };
    callbackId = video.requestVideoFrameCallback(observe);
  };
  const timestampForCycle = (cycleStarted: number): VideoFrameTimestamp => {
    const mediaTimeS = Number(video.currentTime);
    if (latest && Number.isFinite(mediaTimeS)) {
      const mediaAdvanceS = mediaTimeS - latest.mediaTimeS;
      if (mediaAdvanceS > 0) {
        return {
          epochMs: latest.epochMs + mediaAdvanceS * 1_000,
          mediaTimeS,
          presentedFrames: latest.presentedFrames,
          source: "mediaTimeProjection",
        };
      }
      return latest;
    }
    return {
      epochMs: performance.timeOrigin + cycleStarted,
      mediaTimeS: Number.isFinite(mediaTimeS) ? mediaTimeS : 0,
      presentedFrames: 0,
      source: "analysisCycleFallback",
    };
  };
  return { start, stop, timestampForCycle };
}
