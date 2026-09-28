# Build from the exact offline VoiceChat image already validated at the site.
# Build context is the patched speech source directory, not voice-service-agent.
ARG VOICECHAT_BASE_IMAGE
FROM ${VOICECHAT_BASE_IMAGE}
COPY s2s/audio_server.py s2s/prompt_template.jinja /s2s/
ENV USE_JINJA_TEMPLATE_PROMPT=1
