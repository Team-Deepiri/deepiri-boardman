export function normalizeAssistantMarkdown(content: string): string {
  return content.replace(/\r\n?/g, "\n").replace(/\\n/g, "\n");
}
