import { useEffect, useState } from "react";
import { Button, Drawer, Input, Select, Space } from "antd";
import { api, VoiceProfile } from "./api";

const defaultProfile: VoiceProfile = {
  id: "default-naonao", name: "闹闹", description: "一个开朗，幽默的语音伙伴",
  language_style: "说话简短而自信，像朋友之间的聊天",
  persona_instructions: "你是闹闹，一个开朗，幽默的语音伙伴，说话简短而自信，像朋友之间的聊天", is_default: true,
};

export function VoiceProfiles({ cid, selected, onSelect, ensureConversation, disabled }: {
  cid: string; selected: string; onSelect: (id: string) => void;
  ensureConversation: () => Promise<string>; disabled: boolean;
}) {
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [profiles, setProfiles] = useState<VoiceProfile[]>([defaultProfile]);
  const [editing, setEditing] = useState<VoiceProfile | null>(defaultProfile);
  const [name, setName] = useState(defaultProfile.name), [description, setDescription] = useState(defaultProfile.description);
  const [style, setStyle] = useState(defaultProfile.language_style);
  const query = (id: string) => `?conversation_id=${encodeURIComponent(id)}`;
  function edit(profile: VoiceProfile | null) {
    setEditing(profile); setName(profile?.name || ""); setDescription(profile?.description || "");
    setStyle(profile?.language_style || ""); setError("");
  }
  useEffect(() => {
    let current = true;
    setProfiles([defaultProfile]); edit(defaultProfile); onSelect(defaultProfile.id);
    if (cid) void api<VoiceProfile[]>("/voice-profiles" + query(cid)).then((items) => {
      if (current) setProfiles(items);
    }).catch((e) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [cid, onSelect]);
  async function show() {
    setBusy(true); setError("");
    try {
      const id = await ensureConversation();
      if (!id) return;
      setProfiles(await api<VoiceProfile[]>("/voice-profiles" + query(id)));
      setOpen(true);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function save(remove = false) {
    if (!cid) return;
    setBusy(true); setError("");
    try {
      if (remove && editing && !editing.is_default) {
        await api(`/voice-profiles/${editing.id}` + query(cid), { method: "DELETE" });
        onSelect(defaultProfile.id); edit(defaultProfile);
      } else {
        const result = await api<VoiceProfile>(`/voice-profiles${editing && !editing.is_default ? "/" + editing.id : ""}` + query(cid), {
          method: editing && !editing.is_default ? "PATCH" : "POST",
          body: JSON.stringify({ name, description, language_style: style }),
        });
        onSelect(result.id); edit(result);
      }
      setProfiles(await api<VoiceProfile[]>("/voice-profiles" + query(cid)));
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const preview = editing?.is_default ? defaultProfile.persona_instructions
    : ["你是" + name, description, style].map((p) => p.trim()).filter(Boolean).join("，");
  const policy = profiles[0]?.instructions?.slice(profiles[0].persona_instructions.length + 2) || "";
  return <>
    <Button onClick={() => void show()} disabled={disabled || busy}>Voice settings</Button>
    {error && !open && <p role="alert">{error}</p>}
    <Drawer title="Voice settings" open={open} onClose={() => setOpen(false)} width={440}>
      <p>Styles apply to the next voice session. Tool rules and permissions are managed by the server.</p>
      <p>In validation mode, saved profiles belong to the current call identity.</p>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Select aria-label="Voice profile" style={{ width: "100%" }} value={selected}
          options={profiles.map((p) => ({ label: p.name, value: p.id }))} disabled={busy}
          onChange={(id) => { onSelect(id); edit(profiles.find((p) => p.id === id) || defaultProfile); }} />
        <Space><Button onClick={() => edit(null)} disabled={busy}>New profile</Button>
          <Button onClick={() => { onSelect(defaultProfile.id); edit(defaultProfile); }} disabled={busy}>Restore default</Button></Space>
        <label>Profile name<Input aria-label="Profile name" value={name} maxLength={80}
          disabled={editing?.is_default || busy} onChange={(e) => setName(e.target.value)} /></label>
        <label>Description<Input.TextArea aria-label="Profile description" value={description} maxLength={800}
          disabled={editing?.is_default || busy} onChange={(e) => setDescription(e.target.value)} /></label>
        <label>Language style<Input.TextArea aria-label="Language style" value={style} maxLength={800}
          disabled={editing?.is_default || busy} onChange={(e) => setStyle(e.target.value)} /></label>
        <label>Final Instructions preview<Input.TextArea aria-label="Final Instructions preview" readOnly rows={8}
          value={preview + (policy ? "\n\n" + policy : "")} /></label>
        <p>The server appends fixed delegation instructions for the configured transport and language.</p>
        <Space><Button onClick={() => void save()} disabled={busy || !name.trim() || editing?.is_default}>Save profile</Button>
          <Button danger onClick={() => void save(true)} disabled={busy || !editing || editing.is_default}>Delete profile</Button></Space>
        {error && <p role="alert">{error}</p>}
      </Space>
    </Drawer>
  </>;
}
