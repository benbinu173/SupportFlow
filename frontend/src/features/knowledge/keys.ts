export const knowledgeKeys = {
  all: ['knowledge'] as const,
  documents: (query: string, offset: number) => [...knowledgeKeys.all, 'documents', query, offset] as const,
  search: () => [...knowledgeKeys.all, 'search'] as const,
}
