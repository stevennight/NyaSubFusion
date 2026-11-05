// 全局设置：大模型接口（Key 存系统凭据库）、用途分配、预设、下载镜像。只放跟这台电脑有关的，所有项目共用。
import { useEffect, useState } from "react";
import { api } from "../api";
import { GpuComponents } from "../components/GpuComponents";
import { useStore } from "../store";
import type { Cfg, PresetInfo, Provider, Settings, Style } from "../types";
import { deepGet, setIn } from "../util";

// 常用服务：一键填入地址和推荐模型（模型名以服务商文档为准，可以改）
const PRESETS: (Provider & { label: string })[] = [
  { label: "DeepSeek", name: "deepseek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { label: "阿里百炼", name: "bailian", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-max", vision_model: "qwen-vl-max" },
  { label: "OpenAI", name: "openai", base_url: "https://api.openai.com/v1", model: "" },
  { label: "OpenRouter", name: "openrouter", base_url: "https://openrouter.ai/api/v1", model: "" },
  { label: "本地 Ollama", name: "ollama", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:14b" },
];

export function SettingsPage({ onEditPreset }: { onEditPreset: (id: string) => void }) {
  const s = useStore();
  const [st, setSt] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Cfg>({});
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [tests, setTests] = useState<Record<string, string>>({});
  const load = async () => { const r = await api.settings(); setSt(r); setDraft(r.user_config); };
  useEffect(() => { load().catch((e) => s.setError(e.message)); }, []);
  if (!st) return <div className="empty">加载中…</div>;

  const providers: Provider[] = (deepGet(draft, "llm.providers") as Provider[]) ?? [];
  const setProviders = (ps: Provider[]) => setDraft(setIn(draft, "llm.providers", ps));
  const val = (path: string) => (deepGet(draft, path) ?? deepGet(st.effective, path)) as string;
  const dirty = JSON.stringify(draft) !== JSON.stringify(st.user_config);

  const save = async () => {
    try { const r = await api.putSettings(draft); setSt(r); setDraft(r.user_config); await s.reload(); s.toast("设置已保存"); }
    catch (e) { s.setError((e as Error).message); }
  };
  const saveKey = async (name: string) => {
    if (dirty) await save();
    await api.putKey(name, keys[name]);
    setKeys({ ...keys, [name]: "" });
    await load(); await s.reload();
    s.toast(`「${name}」的 API Key 已存入系统凭据库`);
  };
  const test = async (p: Provider) => {
    setTests({ ...tests, [p.name]: "测试中…" });
    const r = await api.testProvider(keys[p.name] ? { ...p, key: keys[p.name] } : { name: p.name, base_url: p.base_url, model: p.model });
    setTests({ ...tests, [p.name]: r.ok ? `✓ 连接成功，${r.ms} ms（回复：${r.reply}）` : `✗ ${r.error}` });
  };
  const add = (p: Provider) => {
    let name = p.name, i = 2;
    while (providers.some((x) => x.name === name)) name = `${p.name}${i++}`;
    const ps = [...providers, { name, base_url: p.base_url, model: p.model, ...(p.vision_model ? { vision_model: p.vision_model } : {}) }];
    let d = setIn(draft, "llm.providers", ps);
    if (!deepGet(d, "llm.translate")) d = setIn(d, "llm.translate", name);
    setDraft(d);
  };
  const upd = (i: number, patch: Partial<Provider>) => setProviders(providers.map((p, k) => (k === i ? { ...p, ...patch } : p)));

  return (
    <div className="page"><div className="wide">
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>设置</h2><span className="dim">所有项目共用 · {st.path}</span><span className="sp" />
        {dirty && <span className="chip warn">未保存</span>}
        <button className="btn" disabled={!dirty} onClick={() => setDraft(st.user_config)}>还原</button>
        <button className="btn pri" disabled={!dirty} onClick={save}>保存设置</button>
      </div>

      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>大模型接口</h3>
          <span className="dim" style={{ fontSize: 12 }}>任何 OpenAI 兼容接口；API Key 存在系统凭据库（{st.keyring}），不写进配置文件</span>
        </div>
        <div className="row wrap" style={{ marginBottom: 10 }}>
          <span className="lbl">添加：</span>
          {PRESETS.map((p) => <button key={p.label} className="btn sm" onClick={() => add(p)}>＋ {p.label}</button>)}
          <button className="btn sm" onClick={() => add({ name: "custom", base_url: "", model: "" })}>＋ 自定义</button>
        </div>
        {providers.length === 0 && <div className="dim" style={{ padding: "6px 0 10px" }}>还没有接口。没有接口时，翻译会导出任务文件，交给 Claude Code 之类的助手完成。</div>}
        {providers.map((p, i) => (
          <div key={i} className="card" style={{ background: "var(--bg2)", marginBottom: 8, padding: 12 }}>
            <div className="kv">
              <span className="lbl">名字</span>
              <div className="row"><input className="inp" style={{ maxWidth: 180 }} value={p.name} onChange={(e) => upd(i, { name: e.target.value.trim() })} />
                {val("llm.translate") === p.name && <span className="chip acc">翻译</span>}
                {(val("llm.vision") || val("llm.translate")) === p.name && <span className="chip">视觉复核</span>}
                {(val("llm.listen") ? val("llm.listen") === p.name : val("llm.translate") === p.name && !!p.listen_model) && <span className="chip">听音</span>}
                <span className="sp" /><button className="btn sm ghost" onClick={() => setProviders(providers.filter((_, k) => k !== i))}>删除</button></div>
              <span className="lbl">接口地址</span><input className="inp" value={p.base_url} placeholder="https://…/v1" onChange={(e) => upd(i, { base_url: e.target.value })} />
              <span className="lbl">模型</span>
              <div className="row"><input className="inp" value={p.model} placeholder="翻译用的模型" onChange={(e) => upd(i, { model: e.target.value })} />
                <input className="inp" value={p.vision_model ?? ""} placeholder="视觉模型（可选）" onChange={(e) => upd(i, { vision_model: e.target.value })} />
                <input className="inp" value={p.listen_model ?? ""} placeholder="听音模型（可选）" title="翻译页勾了“参考语音”时，用这个模型听对话原声，标出问句还是陈述、语气。要能听音频的模型，比如 OpenRouter 上的 google/gemini-4-argon（测试里判断最准；flash 便宜但常把讲解听成问句）。gpt-6-luna 之类不能听音频。" onChange={(e) => upd(i, { listen_model: e.target.value })} /></div>
              <span className="lbl">API Key</span>
              <div className="row">
                {st.keys[p.name] ? <span className="chip ok">已设置</span> : st.env_keys[p.name] ? <span className="chip ok">来自环境变量</span> : <span className="chip warn">未设置</span>}
                <input className="inp" type="password" autoComplete="off" placeholder={st.keys[p.name] ? "输入新的 Key 可替换" : "粘贴 API Key"} value={keys[p.name] ?? ""}
                       onChange={(e) => setKeys({ ...keys, [p.name]: e.target.value })} style={{ flex: 1 }} />
                <button className="btn sm" disabled={!keys[p.name]} onClick={() => saveKey(p.name)}>保存 Key</button>
                {st.keys[p.name] && <button className="btn sm ghost" onClick={async () => { await api.deleteKey(p.name); await load(); s.toast("已删除 Key"); }}>删除 Key</button>}
                <button className="btn sm" onClick={() => test(p)}>测试连接</button>
              </div>
              <span className="lbl">环境变量</span>
              <input className="inp" style={{ maxWidth: 260 }} placeholder="可选：凭据库里没有时从这个环境变量取" value={p.api_key_env ?? ""} onChange={(e) => upd(i, { api_key_env: e.target.value })} />
            </div>
            {tests[p.name] && <div style={{ marginTop: 6, fontSize: 12, color: tests[p.name].startsWith("✓") ? "var(--ok)" : tests[p.name].startsWith("✗") ? "var(--bad)" : "var(--tx2)", wordBreak: "break-all" }}>{tests[p.name]}</div>}
          </div>
        ))}
        <div className="kv" style={{ marginTop: 6 }}>
          <span className="lbl">翻译用</span>
          <select className="inp" style={{ maxWidth: 260 }} value={val("llm.translate") ?? ""} onChange={(e) => setDraft(setIn(draft, "llm.translate", e.target.value))}>
            <option value="">（不用接口列表，按旧的 [llm] 配置）</option>{providers.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select>
          <span className="lbl">画面字复核用</span>
          <select className="inp" style={{ maxWidth: 260 }} value={val("llm.vision") ?? ""} onChange={(e) => setDraft(setIn(draft, "llm.vision", e.target.value))}>
            <option value="">和翻译用同一个</option>{providers.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select>
          <span className="lbl">云端语音识别用</span>
          <select className="inp" style={{ maxWidth: 260 }} value={val("llm.asr") ?? ""} onChange={(e) => setDraft(setIn(draft, "llm.asr", e.target.value))}>
            <option value="">和翻译用同一个</option>{providers.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select>
          <span className="lbl">听音标注用</span>
          <select className="inp" style={{ maxWidth: 260 }} value={val("llm.listen") ?? ""} onChange={(e) => setDraft(setIn(draft, "llm.listen", e.target.value))}>
            <option value="">和翻译用同一个（要填了听音模型）</option>{providers.map((p) => <option key={p.name} value={p.name}>{p.name}{p.listen_model ? ` · ${p.listen_model}` : ` · ${p.model}`}</option>)}</select>
          <span /><span className="hint">一集里可以单独换（在「处理」的对应步骤里），不换就用这里的。听音标注只在翻译页勾了“参考语音”时用。</span>
          <span className="lbl">当前翻译</span>
          <span className="dim">{st.translate.model ? `${st.translate.name || "旧配置"} · ${st.translate.model} @ ${st.translate.base_url}` : "未配置 → 导出任务文件"}</span>
        </div>
      </div>
      <div className="gap" />
      <PresetsCard onEdit={onEditPreset} />
      <div className="gap" />
      <GpuComponents />
      <div className="gap" />
      <div className="card">
        <h3>下载</h3>
        <div className="kv">
          <span className="lbl">模型镜像</span>
          <input className="inp" style={{ maxWidth: 360 }} placeholder="可不填：Hugging Face 连不上时会自动改从 ModelScope 下载" value={val("asr.hf_endpoint") ?? ""}
                 onChange={(e) => setDraft(setIn(draft, "asr.hf_endpoint", e.target.value))} />
        </div>
      </div>
    </div></div>
  );
}

/** 预设：内置的只读（可以复制成自己的），自己的能改、删、设成新项目默认；旁边列出出厂样式 */
function PresetsCard({ onEdit }: { onEdit: (id: string) => void }) {
  const s = useStore();
  const [list, setList] = useState<PresetInfo[]>([]);
  const [factory, setFactory] = useState<Record<string, Style>>({});
  useEffect(() => {
    api.presets().then(setList).catch((e) => s.setError(e.message));
    api.preset("custom").then((p) => setFactory(p.factory.styles)).catch(() => {});
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (f: () => Promise<PresetInfo[]>) => { try { setList(await f()); } catch (e) { s.setError((e as Error).message); } };
  const copy = async (p: PresetInfo) => {
    const name = prompt("自己的预设叫什么？", `${p.name}（我的）`);
    if (!name) return;
    try { const r = await api.createPreset({ name, from_preset: p.id }); setList(r.list); s.toast(`已复制成「${name}」`); } catch (e) { s.setError((e as Error).message); }
  };
  const mine = list.filter((p) => !p.builtin), builtin = list.filter((p) => p.builtin);
  return (
    <div className="grid2" style={{ gridTemplateColumns: "1.25fr 1fr", alignItems: "start" }}>
      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}><h3 style={{ margin: 0 }}>预设</h3>
          <span className="hint">新建项目时选；建好后复制一份给那一集，之后各自独立。一集的设置也能在页头“存成预设…”</span></div>
        <div className="dim" style={{ fontSize: 11.5, margin: "4px 0" }}>我的</div>
        {mine.length === 0 && <div className="dim" style={{ padding: "4px 10px 8px" }}>还没有。可以复制下面的内置预设，或者在一集里“存成预设…”。</div>}
        {mine.map((p) => (
          <div key={p.id} className="prow-mini">
            <b>{p.name}</b>{p.default && <span className="chip acc">新项目默认用它</span>}<span className="dim" style={{ fontSize: 11.5 }}>{p.desc}</span><span className="sp" />
            <button className="btn sm" onClick={() => onEdit(p.id)}>编辑</button>
            {!p.default && <button className="btn sm ghost" onClick={() => act(() => api.defaultPreset(p.id))}>设为默认</button>}
            <button className="btn sm ghost" onClick={() => { if (confirm(`删除预设「${p.name}」？（会先备份；用它建的项目不受影响）`)) act(() => api.deletePreset(p.id)); }}>删除</button>
          </div>
        ))}
        <div className="dim" style={{ fontSize: 11.5, margin: "10px 0 4px" }}>内置（只读，随软件更新）</div>
        {builtin.map((p) => (
          <div key={p.id} className="prow-mini light">
            <span className="muted">{p.name}</span>{p.default && <span className="chip acc">新项目默认用它</span>}<span className="dim" style={{ fontSize: 11.5 }}>{p.desc}</span><span className="sp" />
            {!p.default && <button className="btn sm ghost" onClick={() => act(() => api.defaultPreset(p.id))}>设为默认</button>}
            <button className="btn sm ghost" onClick={() => copy(p)}>复制成我的</button>
          </div>
        ))}
      </div>
      <div className="card">
        <h3>出厂样式 <span className="dim" style={{ fontWeight: 400 }}>只读</span></h3>
        <div className="usedby">{Object.entries(factory).map(([k, f]) => (
          <div key={k}><span className="swatch" style={{ color: f.color, textShadow: `0 0 2px ${f.outline_color}` }}>字</span>{k}<span className="dim">{f.font} {f.size}</span></div>
        ))}</div>
        <div className="hint" style={{ marginTop: 8 }}>软件自带，改不了也删不掉。每一集、每个预设的样式都是从这里复制出去的：改乱了可以在样式上点“恢复出厂”，删掉了可以“＋ 新样式 › 从出厂样式添加”。</div>
      </div>
    </div>
  );
}
