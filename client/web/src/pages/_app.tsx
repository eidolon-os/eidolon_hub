import type { AppProps } from "next/app";
import "@/styles/globals.css";
import "@livekit/components-styles";

export default function App({ Component, pageProps }: AppProps) {
  return <Component {...pageProps} />;
}
