import { useCallback, useMemo } from "react";

export interface AppConfig {
  livekitUrl: string;
  appTitle: string;
  daemonWsUrl: string;
}

export function useConfig() {
  const config: AppConfig = useMemo(() => ({
    livekitUrl: process.env.NEXT_PUBLIC_LIVEKIT_URL ?? "",
    appTitle: process.env.NEXT_PUBLIC_APP_TITLE ?? "Eidolon Agent",
    daemonWsUrl: process.env.NEXT_PUBLIC_DAEMON_WS_URL ?? "ws://localhost:8080/ws",
  }), []);

  const validate = useCallback((): string | null => {
    if (!config.livekitUrl) {
      return "LiveKit URL is not configured. Set NEXT_PUBLIC_LIVEKIT_URL in your environment.";
    }
    return null;
  }, [config]);

  return { config, validate };
}
