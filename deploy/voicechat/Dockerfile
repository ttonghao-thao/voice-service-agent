# Build from the exact offline VoiceChat image already validated at the site.
# Build context is the patched speech source directory, not voice-service-agent.
ARG VOICECHAT_BASE_IMAGE
FROM ${VOICECHAT_BASE_IMAGE}
COPY s2s/audio_server.py s2s/prompt_template.jinja /s2s/
COPY opt/tritonserver/backends/nemotron-voicechat/model.py opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py opt/tritonserver/backends/nemotron-voicechat/data_types.py /opt/tritonserver/backends/nemotron-voicechat/
ENV USE_JINJA_TEMPLATE_PROMPT=1 VOICECHAT_DIAGNOSTICS=1
