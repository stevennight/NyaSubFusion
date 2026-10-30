import { Component, type ReactNode } from "react";

/** 一个页面出错时只让这一页显示错误，导航栏照常能用（不然整个界面会变成一片黑）。换页面时（key 变了）重新渲染。 */
export class PageGuard extends Component<{ children: ReactNode; onBack: () => void }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error("页面出错", error);
  }

  render() {
    const e = this.state.error;
    if (!e) return this.props.children;
    return (
      <div className="card" style={{ margin: 16 }}>
        <h3>这一页出错了</h3>
        <div className="dim" style={{ marginBottom: 10 }}>界面的其他部分还能用。把下面的错误信息发给开发者，能更快修好。</div>
        <pre style={{ whiteSpace: "pre-wrap", maxHeight: 240, overflow: "auto", background: "var(--panel2)", padding: 8, borderRadius: 6, fontSize: 12 }}>{String(e.stack || e.message)}</pre>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn" onClick={() => this.setState({ error: null })}>再试一次</button>
          <button className="btn" onClick={() => { this.setState({ error: null }); this.props.onBack(); }}>回到项目列表</button>
        </div>
      </div>
    );
  }
}
