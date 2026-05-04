import React from "react";

export interface Message {
  id: string;
  name: string;
  message: string;
  timestamp: number;
  isSelf: boolean;
}

interface ChatMessageProps {
  message: Message;
}

export function ChatMessage({ message }: ChatMessageProps) {
  const time = new Date(message.timestamp).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className={`flex ${message.isSelf ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-xs lg:max-w-md px-4 py-2 rounded-2xl text-sm ${
          message.isSelf
            ? "bg-primary-500 text-white rounded-br-md"
            : "bg-white border border-slate-200 text-slate-800 rounded-bl-md"
        }`}
      >
        {!message.isSelf && (
          <p className="text-xs font-medium text-slate-500 mb-0.5">
            {message.name}
          </p>
        )}
        <p className="break-words">{message.message}</p>
        <p
          className={`text-xs mt-1 ${
            message.isSelf ? "text-primary-100" : "text-slate-400"
          }`}
        >
          {time}
        </p>
      </div>
    </div>
  );
}
