# syntax=docker/dockerfile:1.7
# Build from the exact offline VoiceChat image already validated at the site.
# The default context is patched speech; agent_repo is a named context.
ARG VOICECHAT_BASE_IMAGE
FROM ${VOICECHAT_BASE_IMAGE}
COPY s2s/audio_server.py s2s/prompt_template.jinja /s2s/
COPY clients/voicechat/nemotron-voicechat-client.py /opt/voice-validation/nemotron-voicechat-client.py
COPY --from=agent_repo scripts/inspect_voicechat_artifacts.py /opt/voice-validation/inspect_voicechat_artifacts.py
COPY --from=agent_repo artifacts/native-cuekb/cases.json /opt/voice-validation/native-cuekb/cases.json
COPY --from=agent_repo artifacts/native-cuekb/*.wav /opt/voice-validation/native-cuekb/
ENV USE_JINJA_TEMPLATE_PROMPT=1
