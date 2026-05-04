import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { motion, AnimatePresence } from "framer-motion";
import { buildEnvelope, encodeBlobToOpusFrames, sleep } from "@/utils/audio";

type PttStatus =
  | "idle"
  | "connecting"
  | "ready"
  | "recording"
  | "processing"
  | "playing"
  | "done"
  | "error";

interface TranscriptMessage {
  id: string;
  name: string;
  message: string;
  isSelf: boolean;
  timestamp: number;
}

interface WsEnvelope {
  type: string;
  version: string;
  transport: string;
  payload: Record<string, unknown>;
}

export function PushToTalkMode({
  daemonWsUrl,
  onDisconnect,
}: {
  daemonWsUrl: string;
  onDisconnect: () => void;
}) {
  const [status, setStatus] = useState<PttStatus>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [recordDuration, setRecordDuration] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const recordTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const recordStartRef = useRef<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const mediaSourceRef = useRef<MediaSource | null>(null);
  const mediaSourceBufferRef = useRef<SourceBuffer | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioStreamRef = useRef<MediaStream | null>(null);
  const isPlayingRef = useRef(false);
  const audioChunksRef = useRef<Int8Array[]>([]);

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };

  const cleanup = useCallback(() => {
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (recordTimerRef.current) {
      clearInterval(recordTimerRef.current);
      recordTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close();
      audioCtxRef.current = null;
    }
    if (audioStreamRef.current) {
      audioStreamRef.current.getTracks().forEach((t) => t.stop());
      audioStreamRef.current = null;
    }
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      mediaRecorderRef.current.stop();
    }
    mediaRecorderRef.current = null;
    mediaSourceRef.current = null;
    mediaSourceBufferRef.current = null;
    sessionIdRef.current = null;
    isPlayingRef.current = false;
    audioChunksRef.current = [];
  }, []);

  useEffect(() => {
    startTimeRef.current = Date.now();
    timerRef.current = setInterval(() => {
      if (startTimeRef.current) {
        setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }
    }, 1000);
    return cleanup;
  }, [cleanup]);

  const appendTranscript = useCallback((name: string, text: string, isSelf: boolean) => {
    setMessages((prev) => [
      ...prev,
      {
        id: `${Date.now()}-${Math.random()}`,
        name,
        message: text,
        isSelf,
        timestamp: Date.now(),
      },
    ]);
  }, []);

  const initAudioContext = useCallback(() => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new AudioContext({ sampleRate: 16000 });
    }
    if (audioCtxRef.current.state === "suspended") {
      audioCtxRef.current.resume();
    }
  }, []);

  const initMediaSource = useCallback(() => {
    if (mediaSourceRef.current) return;
    const ms = new MediaSource();
    mediaSourceRef.current = ms;
    const url = URL.createObjectURL(ms);
    const audio = new Audio(url);
    audio.autoplay = true;
    audio.id = "ptt-audio-player";
    audio.style.display = "none";
    document.body.appendChild(audio);

    ms.addEventListener("sourceopen", () => {
      const buffer = ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
      buffer.mode = "sequence";
      buffer.addEventListener("updateend", () => {
        if (isPlayingRef.current && audioCtxRef.current?.state === "suspended") {
          audioCtxRef.current.resume();
        }
      });
      mediaSourceBufferRef.current = buffer;

      const pending = audioChunksRef.current.splice(0);
      for (const chunk of pending) {
        if (buffer.updating) {
          buffer.appendBuffer(chunk.buffer as ArrayBuffer);
        }
      }
    });
  }, []);

  const connectWs = useCallback(() => {
    setStatus("connecting");
    setErrorMsg(null);

    const ws = new WebSocket(daemonWsUrl);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(buildEnvelope("handshake", {}));
    };

    ws.onmessage = (event) => {
      if (typeof event.data === "string") {
        const env: WsEnvelope = JSON.parse(event.data);
        handleServerMessage(env);
      } else {
        handleAudioFrame(event.data as ArrayBuffer);
      }
    };

    ws.onerror = () => {
      setErrorMsg("WebSocket connection error");
      setStatus("error");
    };

    ws.onclose = (ev) => {
      if (pingIntervalRef.current) {
        clearInterval(pingIntervalRef.current);
        pingIntervalRef.current = null;
      }
      if (status !== "error") {
        setErrorMsg(`Connection closed (code: ${ev.code})`);
        setStatus("error");
      }
    };
  }, [daemonWsUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleServerMessage = useCallback((env: WsEnvelope) => {
    const { type, payload } = env;

    switch (type) {
      case "connected": {
        wsRef.current?.send(buildEnvelope("handshake", {}));
        break;
      }

      case "handshake": {
        if (payload.state === "ok") {
          wsRef.current?.send(buildEnvelope("listen", { mode: "manual", state: "start" }));
        }
        break;
      }

      case "listening_ok": {
        sessionIdRef.current = payload.session_id as string;
        pingIntervalRef.current = setInterval(() => {
          wsRef.current?.send(buildEnvelope("ping", { timestamp: Date.now() / 1000 }));
        }, 25000);
        setStatus("ready");
        break;
      }

      case "session_start": {
        initAudioContext();
        initMediaSource();
        break;
      }

      case "llm_text": {
        if (payload.text) {
          appendTranscript("Agent", payload.text as string, false);
        }
        break;
      }

      case "agent": {
        if (payload.text) {
          appendTranscript("Agent", payload.text as string, false);
        }
        break;
      }

      case "stop_playing": {
        isPlayingRef.current = false;
        audioChunksRef.current = [];
        const audio = document.getElementById("ptt-audio-player") as HTMLAudioElement | null;
        if (audio) {
          audio.pause();
          audio.currentTime = 0;
        }
        if (mediaSourceRef.current && mediaSourceRef.current.readyState === "open") {
          const buffer = mediaSourceBufferRef.current;
          if (buffer) {
            try {
              buffer.abort();
            } catch {
              // ignore
            }
          }
        }
        break;
      }

      case "session_end": {
        setStatus("done");
        isPlayingRef.current = false;
        break;
      }

      case "error": {
        const code = payload.code as string;
        const msg = payload.message as string | undefined;
        setErrorMsg(`Server error [${code}]: ${msg ?? "unknown"}`);
        setStatus("error");
        break;
      }

      case "close": {
        setErrorMsg(`Server closed: ${(payload.reason as string) ?? "unknown"}`);
        setStatus("error");
        break;
      }

      case "pong": {
        break;
      }
    }
  }, [appendTranscript, initAudioContext, initMediaSource]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleAudioFrame = useCallback((data: ArrayBuffer) => {
    if (status !== "playing") {
      setStatus("playing");
    }

    initAudioContext();
    initMediaSource();

    const int8 = new Int8Array(data);
    audioChunksRef.current.push(int8);

    const buffer = mediaSourceBufferRef.current;
    if (buffer && !buffer.updating) {
      try {
        buffer.appendBuffer(int8.buffer as ArrayBuffer);
      } catch {
        // Buffer full or not ready yet, chunk stored in pending list
      }
    }
  }, [status, initAudioContext, initMediaSource]); // eslint-disable-line react-hooks/exhaustive-deps

  const sendFrames = useCallback(async (frames: Int8Array[]) => {
    for (const frame of frames) {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(frame);
        await sleep(20);
      }
    }
  }, []);

  const startRecording = useCallback(async () => {
    if (status !== "ready" && status !== "done") return;

    if (!sessionIdRef.current) {
      setErrorMsg("Session not ready. Please wait and try again.");
      return;
    }

    setRecordDuration(0);
    recordStartRef.current = Date.now();
    recordTimerRef.current = setInterval(() => {
      if (recordStartRef.current) {
        setRecordDuration(Math.floor((Date.now() - recordStartRef.current) / 1000));
      }
    }, 1000);

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioStreamRef.current = stream;

      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: "audio/webm;codecs=opus",
      });
      mediaRecorderRef.current = mediaRecorder;

      mediaRecorder.ondataavailable = async (e) => {
        if (e.data && e.data.size > 0) {
          try {
            const frames = await encodeBlobToOpusFrames(e.data);
            await sendFrames(frames);
          } catch {
            // Skip chunks that fail to encode
          }
        }
      };

      mediaRecorder.onstop = () => {
        if (recordTimerRef.current) {
          clearInterval(recordTimerRef.current);
          recordTimerRef.current = null;
        }
        if (audioStreamRef.current) {
          audioStreamRef.current.getTracks().forEach((t) => t.stop());
          audioStreamRef.current = null;
        }
        wsRef.current?.send(
          buildEnvelope("record_end", { session_id: sessionIdRef.current })
        );
        setStatus("processing");
      };

      mediaRecorder.start(200);
      setStatus("recording");
    } catch (err) {
      if (recordTimerRef.current) {
        clearInterval(recordTimerRef.current);
        recordTimerRef.current = null;
      }
      const msg = err instanceof Error ? err.message : "Failed to access microphone";
      setErrorMsg(msg);
      setStatus("error");
    }
  }, [status, sendFrames]);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== "inactive") {
      mediaRecorderRef.current.stop();
    }
  }, []);

  useEffect(() => {
    connectWs();
    return cleanup;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const isRecording = status === "recording";
  const isProcessing = status === "processing" || status === "connecting";
  const isPlaying = status === "playing";

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 bg-white border-b border-slate-200">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-purple-100 flex items-center justify-center">
            <svg
              className="w-5 h-5 text-purple-600"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
              />
            </svg>
          </div>
          <div>
            <h2 className="font-semibold text-slate-900">Push to Talk</h2>
            <p className="text-xs text-slate-500">{statusLabel(status)}</p>
          </div>
        </div>
        <div className="text-sm font-mono text-slate-600">
          {formatTime(elapsed)}
        </div>
      </div>

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3 bg-slate-50">
        <AnimatePresence>
          {messages.length === 0 && (status === "idle" || status === "connecting") && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex items-center justify-center h-full"
            >
              <div className="text-center text-slate-400">
                <svg
                  className="w-12 h-12 mx-auto mb-3 text-slate-300"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={1.5}
                    d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                  />
                </svg>
                <p className="text-sm">Hold the button to start recording</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {messages.map((msg) => (
          <ChatBubble key={msg.id} message={msg} />
        ))}
      </div>

      {/* Error */}
      {errorMsg && (
        <div className="mx-4 mb-2 p-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
          {errorMsg}
        </div>
      )}

      {/* PTT Controls */}
      <div className="flex items-center justify-center gap-6 px-6 py-6 bg-white border-t border-slate-200">
        {/* Main PTT button */}
        <div className="relative flex flex-col items-center gap-1">
          <motion.button
            className={`
              w-20 h-20 rounded-full flex items-center justify-center select-none
              transition-colors shadow-lg
              ${isRecording
                ? "bg-red-500 hover:bg-red-600"
                : isProcessing || isPlaying
                ? "bg-slate-300 cursor-not-allowed"
                : "bg-purple-500 hover:bg-purple-600 active:bg-purple-700"
              }
            `}
            disabled={isProcessing || isPlaying}
            onMouseDown={startRecording}
            onMouseUp={stopRecording}
            onMouseLeave={stopRecording}
            onTouchStart={(e) => { e.preventDefault(); startRecording(); }}
            onTouchEnd={(e) => { e.preventDefault(); stopRecording(); }}
            whileTap={isRecording ? { scale: 0.95 } : { scale: 0.98 }}
          >
            {isRecording ? (
              <motion.div
                className="w-8 h-8 rounded bg-white"
                animate={{ opacity: [1, 0.3, 1] }}
                transition={{ repeat: Infinity, duration: 1 }}
              />
            ) : isProcessing ? (
              <svg
                className="w-8 h-8 text-slate-500 animate-spin"
                fill="none"
                viewBox="0 0 24 24"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
            ) : isPlaying ? (
              <svg
                className="w-8 h-8 text-white"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z"
                />
              </svg>
            ) : (
              <svg
                className="w-8 h-8 text-white"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
                />
              </svg>
            )}
          </motion.button>

          {/* Recording duration */}
          {isRecording && (
            <motion.span
              className="text-xs font-mono text-red-500"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            >
              {formatTime(recordDuration)}
            </motion.span>
          )}

          {/* Status hint */}
          {!isRecording && !isProcessing && !isPlaying && status === "ready" && (
            <span className="text-xs text-slate-400">Hold to record</span>
          )}
          {isProcessing && (
            <span className="text-xs text-slate-400">Processing...</span>
          )}
          {isPlaying && (
            <span className="text-xs text-slate-400">Playing...</span>
          )}
        </div>

        {/* Disconnect */}
        <button
          onClick={onDisconnect}
          className="w-14 h-14 rounded-full bg-red-500 text-white flex items-center justify-center hover:bg-red-600 transition-colors shadow-lg"
          title="Disconnect"
        >
          <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M16 8l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2M5 3a2 2 0 00-2 2v1c0 8.284 6.716 15 15 15h1a2 2 0 002-2v-3.28a1 1 0 00-.684-.948l-4.493-1.498a1 1 0 00-1.21.502l-1.13 2.257a11.042 11.042 0 01-5.516-5.517l2.257-1.128a1 1 0 00.502-1.21L9.228 3.683A1 1 0 008.279 3H5z"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}

function statusLabel(status: PttStatus): string {
  switch (status) {
    case "idle": return "Connecting...";
    case "connecting": return "Connecting...";
    case "ready": return "Ready";
    case "recording": return "Recording...";
    case "processing": return "Processing...";
    case "playing": return "Playing response...";
    case "done": return "Response complete";
    case "error": return "Error";
  }
}

function ChatBubble({ message }: { message: TranscriptMessage }) {
  return (
    <div className={`flex ${message.isSelf ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] px-4 py-3 rounded-2xl text-sm ${
          message.isSelf
            ? "bg-primary-500 text-white rounded-br-md"
            : "bg-white border border-slate-200 text-slate-800 rounded-bl-md"
        }`}
      >
        {!message.isSelf && (
          <p className="text-xs font-semibold text-slate-500 mb-1">{message.name}</p>
        )}
        <p className="leading-relaxed">{message.message}</p>
      </div>
    </div>
  );
}
