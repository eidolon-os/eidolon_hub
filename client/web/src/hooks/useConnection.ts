import { useCallback, useRef, useState } from "react";

export type ConnectionMode = "livekit";
export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

export interface ConnectionState {
  status: ConnectionStatus;
  roomName: string | null;
  participantName: string | null;
  identity: string | null;
  token: string | null;
  error: string | null;
}

export function useConnection(livekitUrl: string) {
  const [state, setState] = useState<ConnectionState>({
    status: "idle",
    roomName: null,
    participantName: null,
    identity: null,
    token: null,
    error: null,
  });

  const abortControllerRef = useRef<AbortController | null>(null);

  const connect = useCallback(async (roomName: string, participantName: string) => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const ac = new AbortController();
    abortControllerRef.current = ac;

    setState({
      status: "connecting",
      roomName,
      participantName,
      identity: null,
      token: null,
      error: null,
    });

    try {
      const params = new URLSearchParams({
        roomName,
        participantName,
      });
      const tokenUrl = process.env.NEXT_PUBLIC_LIVEKIT_TOKEN_URL ?? "http://localhost:8000/api/livekit/token";
      const response = await fetch(`${tokenUrl}?${params}`, {
        signal: ac.signal,
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error ?? "Failed to get token");
      }

      const data = await response.json();

      if (ac.signal.aborted) return;

      setState({
        status: "connected",
        roomName,
        participantName,
        identity: data.identity,
        token: data.accessToken,
        error: null,
      });
    } catch (err) {
      if (ac.signal.aborted) return;
      const message = err instanceof Error ? err.message : "Connection failed";
      setState((prev) => ({
        ...prev,
        status: "error",
        error: message,
      }));
    }
  }, []);

  const disconnect = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setState({
      status: "disconnected",
      roomName: null,
      participantName: null,
      identity: null,
      token: null,
      error: null,
    });
  }, []);

  return {
    serverUrl: livekitUrl,
    ...state,
    connect,
    disconnect,
  };
}
