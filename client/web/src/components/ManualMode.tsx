import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  buildEnvelope,
  sleep,
  encodeAudioToOpus,
  WsEnvelope,
} from "@/utils/audio";

type ModeStatus =
  | "idle"
  | "connecting"
  | "uploading"
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

export function ManualMode({
  daemonWsUrl,
  onDisconnect,
}: {
  daemonWsUrl: string;
  onDisconnect: () => void;
}) {
  const [status, setStatus] = useState<ModeStatus>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [messages, setMessages] = useState<TranscriptMessage[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const mediaSourceRef = useRef<MediaSource | null>(null);
  const mediaSourceBufferRef = useRef<SourceBuffer | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
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
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (audioCtxRef.current) {
      audioCtxRef.current.close();
      audioCtxRef.current = null;
    }
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
    audio.id = "manual-audio-player";
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
      if (status !== "error" && status !== "done") {
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
        const audio = document.getElementById("manual-audio-player") as HTMLAudioElement | null;
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

  const handleUpload = useCallback(async () => {
    fileInputRef.current?.click();
  }, []);

  const processFile = useCallback(async (file: File) => {
    if (!sessionIdRef.current) {
      setErrorMsg("Session not ready. Please wait and try again.");
      return;
    }

    setStatus("uploading");
    setErrorMsg(null);

    try {
      const arrayBuffer = await file.arrayBuffer();
      const audioData = new Uint8Array(arrayBuffer);

      const opusFrames = await encodeAudioToOpus(audioData, file.type);

      setStatus("processing");

      wsRef.current?.send(buildEnvelope("record_start", {}));

      for (const frame of opusFrames) {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(frame);
          await sleep(20);
        }
      }

      wsRef.current?.send(
        buildEnvelope("record_end", { session_id: sessionIdRef.current })
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to process audio";
      setErrorMsg(msg);
      setStatus("error");
    }
  }, []);

  const onFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      if (file.size > 50 * 1024 * 1024) {
        setErrorMsg("File too large. Maximum size is 50MB.");
        setStatus("error");
        return;
      }
      processFile(file);
    },
    [processFile]
  );

  useEffect(() => {
    connectWs();
    return cleanup;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 bg-white border-b border-slate-200">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-blue-100 flex items-center justify-center">
            <svg
              className="w-5 h-5 text-blue-600"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"
              />
            </svg>
          </div>
          <div>
            <h2 className="font-semibold text-slate-900">Manual Mode</h2>
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
          {messages.length === 0 && status === "idle" && (
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
                    d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                  />
                </svg>
                <p className="text-sm">Upload an audio file to start</p>
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

      {/* Controls */}
      <div className="flex items-center justify-center gap-4 px-6 py-5 bg-white border-t border-slate-200">
        {status === "idle" || status === "done" || status === "error" ? (
          <button
            onClick={handleUpload}
            className="w-14 h-14 rounded-full bg-blue-500 text-white flex items-center justify-center hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            title="Upload audio"
          >
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
              />
            </svg>
          </button>
        ) : (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
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
            {status === "connecting" && "Connecting..."}
            {status === "uploading" && "Processing audio..."}
            {status === "processing" && "Agent thinking..."}
            {status === "playing" && "Playing response..."}
          </div>
        )}

        <button
          onClick={onDisconnect}
          className="w-14 h-14 rounded-full bg-red-500 text-white flex items-center justify-center hover:bg-red-600 transition-colors"
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

      <input
        ref={fileInputRef}
        type="file"
        accept="audio/wav,audio/mp3,audio/mpeg,audio/ogg,audio/webm,audio/mp4,audio/x-wav,audio/wave"
        className="hidden"
        onChange={onFileChange}
      />
    </div>
  );
}

function statusLabel(status: ModeStatus): string {
  switch (status) {
    case "idle": return "Ready";
    case "connecting": return "Connecting...";
    case "uploading": return "Processing audio...";
    case "processing": return "Agent thinking...";
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

