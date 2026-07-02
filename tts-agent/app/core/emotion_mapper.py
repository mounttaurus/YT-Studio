EMOTION_TO_EMOJI: dict[str, str] = {
    "neutral":   "",
    "happy":     "😊",
    "excited":   "😆",
    "sad":       "😭",
    "serious":   "📖",
    "question":  "🤔",
    "angry":     "😠",
    "surprised": "😲",
    "shy":       "🫣",
    "whisper":   "👂",
    "confident": "😎",
    "worried":   "😟",
    "gentle":    "🫶",
    "fast":      "⏩",
    "slow":      "🐢",
    "narration": "📖",
}


def emotion_to_emoji(emotion: str) -> str:
    return EMOTION_TO_EMOJI.get(emotion, "")


def apply_emotion_to_text(text: str, emotion: str) -> str:
    emoji = emotion_to_emoji(emotion)
    if not emoji:
        return text
    return f"{emoji}{text}"
