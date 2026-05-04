import React, { useEffect, useRef } from "react";
import { TrackReference } from "@livekit/components-react";
import { useTrackVolume } from "@livekit/components-react";

interface AudioVisualizerProps {
  agentAudioTrack?: TrackReference;
}

export function AudioVisualizer({ agentAudioTrack }: AudioVisualizerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const volume = useTrackVolume(agentAudioTrack);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let animationId: number;

    const draw = () => {
      const { width, height } = canvas;
      ctx.clearRect(0, 0, width, height);

      const barCount = 32;
      const barWidth = width / barCount - 2;

      for (let i = 0; i < barCount; i++) {
        const barHeight = Math.max(4, volume * height * (0.3 + Math.random() * 0.7));
        const x = i * (barWidth + 2);
        const y = (height - barHeight) / 2;

        const hue = 200 - volume * 60;
        ctx.fillStyle = `hsl(${hue}, 80%, ${40 + volume * 40}%)`;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, 2);
        ctx.fill();
      }

      animationId = requestAnimationFrame(draw);
    };

    animationId = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(animationId);
  }, [volume]);

  return (
    <canvas
      ref={canvasRef}
      width={240}
      height={48}
      className="w-full max-w-60 h-12"
    />
  );
}
