import { useDeferredValue } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

type MarkdownMessageProps = {
  content: string;
  className?: string;
  isStreaming?: boolean;
};

const markdownComponents: Components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  table: ({ children }) => (
    <div className="markdown-table-wrap">
      <table className="markdown-table">{children}</table>
    </div>
  ),
  code: ({ className, children, ...props }) => {
    if (!className) {
      return (
        <code className="markdown-inline-code" {...props}>
          {children}
        </code>
      );
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => <pre className="markdown-pre">{children}</pre>,
};

export function MarkdownMessage({ content, className, isStreaming = false }: MarkdownMessageProps) {
  const deferredContent = useDeferredValue(content);
  const displayContent = isStreaming ? deferredContent : content;
  const rootClassName = className ? `markdown-body ${className}` : "markdown-body";

  return (
    <div className={rootClassName}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={markdownComponents}>
        {displayContent}
      </ReactMarkdown>
    </div>
  );
}
