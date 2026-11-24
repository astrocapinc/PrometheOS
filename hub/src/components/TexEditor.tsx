// CodeMirror 6 editor for LaTeX (stex mode) with completion of labels and citation keys,
// find/replace, and a gutter marking lines bound to anchors (claims and evidence).
// The document is plain text; nothing typed here is interpreted as HTML.
import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import {
  bracketMatching,
  HighlightStyle,
  StreamLanguage,
  syntaxHighlighting,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { highlightSelectionMatches, openSearchPanel, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorSelection, EditorState, RangeSetBuilder } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  gutter,
  GutterMarker,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";

export interface EditorHandle {
  goTo: (line: number) => void;
  cursorLine: () => number;
  insert: (text: string) => void;
  openSearch: () => void;
  focus: () => void;
  text: () => string;
}

export interface GutterMark {
  line: number;
  label: string;
  status: string;
}

export interface CompletionData {
  labels: string[];
  cites: { key: string; detail: string }[];
}

interface Props {
  /** Changing the key replaces the document (a new file or a reload from disk). */
  docKey: string;
  initial: string;
  /** Line to place the cursor on when the document is (re)created. */
  initialLine?: number;
  crlf: boolean;
  label: string;
  completion: CompletionData;
  marks: GutterMark[];
  onChange: (text: string) => void;
  onSave: () => void;
  onForward: () => void;
}

class AnchorMarker extends GutterMarker {
  constructor(
    readonly text: string,
    readonly status: string,
  ) {
    super();
  }
  eq(other: AnchorMarker): boolean {
    return other.text === this.text && other.status === this.status;
  }
  toDOM(): Node {
    const span = document.createElement("span");
    span.className = `tex-anchor-mark tex-anchor-${this.status}`;
    span.textContent = this.status === "spacer" ? "" : this.status === "current" ? "◆" : "◇";
    if (this.text) {
      span.title = this.text;
      span.setAttribute("aria-label", this.text);
    }
    span.dataset.testid = "anchor-mark";
    return span;
  }
}

// Token colours chosen for WCAG AA contrast on white and on the active-line tint.
const TEX_HIGHLIGHT = HighlightStyle.define([
  { tag: [tags.keyword, tags.tagName, tags.macroName], color: "#1d3f73" },
  { tag: [tags.atom, tags.bracket], color: "#5a2d82" },
  { tag: [tags.comment, tags.lineComment], color: "#4b5563", fontStyle: "italic" },
  { tag: [tags.string, tags.special(tags.string)], color: "#7a2410" },
  { tag: [tags.number, tags.variableName], color: "#155e3b" },
  { tag: tags.heading, fontWeight: "bold", color: "#1c1d1f" },
  { tag: tags.invalid, color: "#a1261d" },
]);

// Styles live in the editor's shadow root (see the effect below), not in styles.css.
const EDITOR_THEME = EditorView.theme({
  "&": {
    height: "100%",
    fontSize: "0.92rem",
    backgroundColor: "#ffffff",
    color: "#1c1d1f",
  },
  "&.cm-focused": { outline: "3px solid #b54708", outlineOffset: "-3px" },
  ".cm-scroller": {
    overflow: "auto",
    fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, Menlo, monospace',
  },
  ".cm-gutters": { backgroundColor: "#f3f4f6", color: "#555a61", borderRight: "1px solid #d6d8dc" },
  ".tex-anchor-gutter": { minWidth: "1.3rem", textAlign: "center" },
  ".tex-anchor-mark": { color: "#1f4f8f", cursor: "help" },
  ".tex-anchor-needs_review": { color: "#7a4a00" },
  ".cm-activeLine": { backgroundColor: "#eef3fb" },
  ".cm-tooltip-autocomplete ul li[aria-selected]": { backgroundColor: "#1f4f8f", color: "#ffffff" },
});

const REF_COMMAND = /ref$/i;
const CITE_COMMAND = /cite/i;

function completionSource(data: () => CompletionData) {
  return (context: CompletionContext): CompletionResult | null => {
    const match = context.matchBefore(/\\([A-Za-z]+)\*?(?:\[[^\]]*\])*\{[^}]*$/);
    if (!match) return null;
    const command = /\\([A-Za-z]+)/.exec(match.text)?.[1] ?? "";
    const brace = match.text.lastIndexOf("{");
    const inner = match.text.slice(brace + 1);
    const from = match.from + brace + 1 + inner.lastIndexOf(",") + 1;
    const current = data();
    if (REF_COMMAND.test(command)) {
      return {
        from,
        options: current.labels.map((label) => ({ label, type: "variable", detail: "label" })),
        validFor: /^[^,}\s]*$/,
      };
    }
    if (CITE_COMMAND.test(command)) {
      return {
        from,
        options: current.cites.map((c) => ({ label: c.key, type: "constant", detail: c.detail })),
        validFor: /^[^,}\s]*$/,
      };
    }
    return null;
  };
}

function markersExtension(marks: GutterMark[]) {
  return gutter({
    class: "tex-anchor-gutter",
    markers: (view) => {
      const builder = new RangeSetBuilder<GutterMarker>();
      const sorted = [...marks].sort((a, b) => a.line - b.line);
      let last = 0;
      for (const mark of sorted) {
        if (mark.line < 1 || mark.line > view.state.doc.lines || mark.line === last) continue;
        const line = view.state.doc.line(mark.line);
        builder.add(line.from, line.from, new AnchorMarker(mark.label, mark.status));
        last = mark.line;
      }
      return builder.finish();
    },
    initialSpacer: () => new AnchorMarker("", "spacer"),
  });
}

export const TexEditor = forwardRef<EditorHandle, Props>(function TexEditor(props, ref) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const gutterSlot = useRef(new Compartment());
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    if (!host.current) return;
    // CodeMirror injects its styles with style-mod. In a document that means a <style>
    // element, which the hub's CSP (style-src 'self', no nonce) blocks; inside a shadow
    // root style-mod uses a constructed stylesheet instead, which CSP permits.
    const shadow = host.current.shadowRoot ?? host.current.attachShadow({ mode: "open" });
    const state = EditorState.create({
      doc: props.initial,
      extensions: [
        props.crlf ? EditorState.lineSeparator.of("\r\n") : [],
        lineNumbers(),
        highlightActiveLineGutter(),
        gutterSlot.current.of(markersExtension(latest.current.marks)),
        history(),
        drawSelection(),
        bracketMatching(),
        closeBrackets(),
        highlightActiveLine(),
        highlightSelectionMatches(),
        StreamLanguage.define(stex),
        syntaxHighlighting(TEX_HIGHLIGHT),
        search({ top: true }),
        autocompletion({
          override: [completionSource(() => latest.current.completion)],
          activateOnTyping: true,
        }),
        EditorView.lineWrapping,
        EDITOR_THEME,
        EditorView.contentAttributes.of({
          "aria-label": props.label,
          "aria-multiline": "true",
          "data-testid": "tex-editor-content",
        }),
        keymap.of([
          {
            key: "Mod-s",
            preventDefault: true,
            run: () => {
              latest.current.onSave();
              return true;
            },
          },
          {
            key: "Mod-Alt-j",
            preventDefault: true,
            run: () => {
              latest.current.onForward();
              return true;
            },
          },
          ...closeBracketsKeymap,
          ...completionKeymap,
          ...searchKeymap,
          ...historyKeymap,
          ...defaultKeymap,
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) latest.current.onChange(update.state.doc.toString());
        }),
      ],
    });
    const editor = new EditorView({ state, parent: shadow, root: shadow });
    view.current = editor;
    // Accessibility checks do not see the editable content inside the shadow root as the
    // scroller's focusable content, so the scroller itself is a labelled tab stop
    // (arrow keys scroll it; the next Tab enters the text).
    editor.scrollDOM.tabIndex = 0;
    editor.scrollDOM.setAttribute("role", "region");
    editor.scrollDOM.setAttribute("aria-label", `${props.label} (scroll area)`);
    const start = props.initialLine;
    if (start && start >= 1) {
      const target = editor.state.doc.line(Math.min(start, editor.state.doc.lines));
      editor.dispatch({
        selection: EditorSelection.cursor(target.from),
        effects: EditorView.scrollIntoView(target.from, { y: "center" }),
      });
      editor.focus();
    }
    return () => {
      editor.destroy();
      view.current = null;
    };
    // The document is replaced only when docKey changes (a new file or a reload).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.docKey, props.crlf]);

  // Reconfigure the gutter only when the marks really change: a reconfiguration on every
  // keystroke would close an open completion list.
  const marksKey = JSON.stringify(props.marks);
  useEffect(() => {
    view.current?.dispatch({
      effects: gutterSlot.current.reconfigure(markersExtension(latest.current.marks)),
    });
  }, [marksKey]);

  useImperativeHandle(ref, () => ({
    goTo: (line: number) => {
      const editor = view.current;
      if (!editor) return;
      const target = editor.state.doc.line(Math.max(1, Math.min(line, editor.state.doc.lines)));
      editor.dispatch({
        selection: EditorSelection.cursor(target.from),
        effects: EditorView.scrollIntoView(target.from, { y: "center" }),
      });
      editor.focus();
    },
    cursorLine: () => {
      const editor = view.current;
      if (!editor) return 1;
      return editor.state.doc.lineAt(editor.state.selection.main.head).number;
    },
    insert: (text: string) => {
      const editor = view.current;
      if (!editor) return;
      editor.dispatch(editor.state.replaceSelection(text));
      editor.focus();
    },
    openSearch: () => {
      if (view.current) openSearchPanel(view.current);
    },
    focus: () => view.current?.focus(),
    text: () => view.current?.state.doc.toString() ?? "",
  }));

  return <div className="tex-editor" ref={host} data-testid="tex-editor" />;
});
