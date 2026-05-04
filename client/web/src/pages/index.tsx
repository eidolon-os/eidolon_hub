import React, { useState, useCallback, useEffect } from "react";
import Head from "next/head";
import { LiveKitRoom, RoomAudioRenderer, StartAudio } from "@livekit/components-react";
import "@livekit/components-styles";
import { useConfig } from "@/hooks/useConfig";
import { useConnection } from "@/hooks/useConnection";
import { ConversationRoom } from "@/components/ConversationRoom";
import { ManualMode } from "@/components/ManualMode";
import { PushToTalkMode } from "@/components/PushToTalkMode";
import { motion } from "framer-motion";

type ConnectionMode = "livekit" | "manual" | "ptt";

function HomeContent() {
  const { config, validate } = useConfig();
  const {
    serverUrl,
    status,
    roomName,
    participantName,
    token,
    identity,
    error,
    connect,
    disconnect,
  } = useConnection(config.livekitUrl);

  const [mode, setMode] = useState<ConnectionMode>("livekit");
  const [manualConnected, setManualConnected] = useState(false);
  const [pttConnected, setPttConnected] = useState(false);
  const [pendingRoom, setPendingRoom] = useState<string>("");
  const [pendingName, setPendingName] = useState<string>("");
  const [validationError, setValidationError] = useState<string | null>(null);

  const handleConnect = useCallback(() => {
    const err = validate();
    if (err) {
      setValidationError(err);
      return;
    }
    setValidationError(null);
    const room = pendingRoom.trim() || `room-${Date.now()}`;
    const name = pendingName.trim() || `user-${Date.now()}`;
    connect(room, name);
  }, [validate, pendingRoom, pendingName, connect]);

  const handleDisconnect = useCallback(() => {
    disconnect();
  }, [disconnect]);

  useEffect(() => {
    if (status === "disconnected" || status === "error") {
      setPendingRoom("");
      setPendingName("");
      setManualConnected(false);
      setPttConnected(false);
    }
  }, [status]);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 flex items-center justify-center p-4">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-md"
      >
        {/* Pre-connection screen */}
        {status !== "connected" && status !== "connecting" && !manualConnected && !pttConnected && (
          <div className="bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden">
            <div className="bg-primary-500 px-6 py-5">
              <h1 className="text-xl font-semibold text-white">
                {config.appTitle}
              </h1>
              <p className="text-primary-100 text-sm mt-1">
                Connect to your voice agent
              </p>
            </div>

            {/* Mode switcher */}
            <div className="flex border-b border-slate-200">
              <button
                onClick={() => setMode("livekit")}
                className={`flex-1 px-4 py-3 text-sm font-medium transition-colors ${
                  mode === "livekit"
                    ? "text-primary-600 border-b-2 border-primary-500"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                Real-time
              </button>
              <button
                onClick={() => setMode("manual")}
                className={`flex-1 px-4 py-3 text-sm font-medium transition-colors ${
                  mode === "manual"
                    ? "text-primary-600 border-b-2 border-primary-500"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                Upload Audio
              </button>
              <button
                onClick={() => setMode("ptt")}
                className={`flex-1 px-4 py-3 text-sm font-medium transition-colors ${
                  mode === "ptt"
                    ? "text-primary-600 border-b-2 border-primary-500"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                Push to Talk
              </button>
            </div>

            {/* LiveKit mode */}
            {mode === "livekit" && (
              <>
                <div className="px-6 py-6 space-y-4">
                  {validationError && (
                    <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
                      {validationError}
                    </div>
                  )}

                  {error && (
                    <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
                      {error}
                    </div>
                  )}

                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1.5">
                      Room Name
                    </label>
                    <input
                      type="text"
                      value={pendingRoom}
                      onChange={(e) => setPendingRoom(e.target.value)}
                      placeholder="e.g., my-session-001"
                      className="w-full px-3 py-2 rounded-lg border border-slate-300 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-slate-700 mb-1.5">
                      Your Name
                    </label>
                    <input
                      type="text"
                      value={pendingName}
                      onChange={(e) => setPendingName(e.target.value)}
                      placeholder="e.g., John"
                      className="w-full px-3 py-2 rounded-lg border border-slate-300 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                  </div>

                  <button
                    onClick={handleConnect}
                    className="w-full py-2.5 rounded-lg bg-primary-500 text-white font-medium text-sm hover:bg-primary-600 transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2"
                  >
                    Start Conversation
                  </button>
                </div>

                <div className="px-6 py-4 bg-slate-50 border-t border-slate-200">
                  <p className="text-xs text-slate-500 text-center">
                    Your browser will request microphone access.
                  </p>
                </div>
              </>
            )}

            {/* Manual mode */}
            {mode === "manual" && (
              <>
                <div className="px-6 py-6 space-y-4">
                  <div className="text-center py-2">
                    <svg
                      className="w-10 h-10 mx-auto mb-3 text-blue-400"
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
                    <h3 className="font-semibold text-slate-800 mb-1">Upload Audio File</h3>
                    <p className="text-sm text-slate-500">
                      Send a pre-recorded audio message to the agent.
                      Supports WAV, MP3, OGG, WebM.
                    </p>
                  </div>

                  <button
                    onClick={() => setManualConnected(true)}
                    className="w-full py-2.5 rounded-lg bg-blue-500 text-white font-medium text-sm hover:bg-blue-600 transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
                  >
                    Start Manual Session
                  </button>
                </div>

                <div className="px-6 py-4 bg-slate-50 border-t border-slate-200">
                  <p className="text-xs text-slate-500 text-center">
                    Audio is processed through the daemon WebSocket pipeline.
                  </p>
                </div>
              </>
            )}

            {/* PTT mode */}
            {mode === "ptt" && (
              <>
                <div className="px-6 py-6 space-y-4">
                  <div className="text-center py-2">
                    <div className="w-16 h-16 mx-auto mb-3 rounded-full bg-purple-100 flex items-center justify-center">
                      <svg
                        className="w-8 h-8 text-purple-500"
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
                    <h3 className="font-semibold text-slate-800 mb-1">Push to Talk</h3>
                    <p className="text-sm text-slate-500">
                      Hold to record your voice and send it to the agent.
                      Release to stop and process.
                    </p>
                  </div>

                  <button
                    onClick={() => setPttConnected(true)}
                    className="w-full py-2.5 rounded-lg bg-purple-500 text-white font-medium text-sm hover:bg-purple-600 transition-colors focus:outline-none focus:ring-2 focus:ring-purple-500 focus:ring-offset-2"
                  >
                    Start PTT Session
                  </button>
                </div>

                <div className="px-6 py-4 bg-slate-50 border-t border-slate-200">
                  <p className="text-xs text-slate-500 text-center">
                    Hold the microphone button to record.
                  </p>
                </div>
              </>
            )}
          </div>
        )}

        {/* Connecting indicator */}
        {status === "connecting" && (
          <div className="bg-white rounded-2xl shadow-xl border border-slate-200 p-8 text-center">
            <div className="flex justify-center mb-4">
              <div className="w-16 h-16 rounded-full bg-primary-100 flex items-center justify-center">
                <svg
                  className="w-8 h-8 text-primary-500 animate-spin"
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
              </div>
            </div>
            <h2 className="text-lg font-semibold text-slate-900 mb-1">
              Connecting...
            </h2>
            <p className="text-sm text-slate-500">
              Joining room <span className="font-medium">{roomName}</span>
            </p>
          </div>
        )}

        {/* Connected — LiveKit Room */}
        {status === "connected" && token && (
          <LiveKitRoom
            serverUrl={serverUrl}
            token={token}
            connect={true}
            audio={true}
            video={false}
            className="bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden h-[600px]"
          >
            <RoomAudioRenderer />
            <StartAudio label="Enable Audio" />
            <ConversationRoom
              participantName={participantName ?? identity ?? "User"}
              onDisconnect={handleDisconnect}
            />
          </LiveKitRoom>
        )}

        {/* Connected — Manual Mode */}
        {manualConnected && (
          <div className="bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden h-[600px]">
            <ManualMode
              daemonWsUrl={config.daemonWsUrl}
              onDisconnect={() => setManualConnected(false)}
            />
          </div>
        )}

        {/* Connected — Push to Talk */}
        {pttConnected && (
          <div className="bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden h-[600px]">
            <PushToTalkMode
              daemonWsUrl={config.daemonWsUrl}
              onDisconnect={() => setPttConnected(false)}
            />
          </div>
        )}
      </motion.div>
    </div>
  );
}

export default function Home() {
  const { config } = useConfig();

  return (
    <>
      <Head>
        <title>{config.appTitle}</title>
        <meta name="description" content="Eidolon Agent Web Client" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="icon" href="/favicon.ico" />
      </Head>
      <HomeContent />
    </>
  );
}
