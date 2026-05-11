# Future Kanban & Workflow Integrations

## Optional Kanban Provider Selection

Support pluggable integrations beyond Plaky so users can choose their preferred workflow management tool:

- **Linear** integration
- **ClickUp** integration
- Other Kanban / workflow management platforms as demand arises

This would be an optional selection — the system should abstract the board provider so swapping or adding new ones is straightforward.

## Local Simulated Kanban Board UI

Build a standalone Kanban board UI that runs locally and **syncs bidirectionally** with the remote board (Plaky, Linear, etc.):

- Not a direct copy of Plaky's UI — a clean, generic Kanban interface
- Drag-and-drop cards, columns, and standard board operations
- Changes made locally push updates to the remote board
- Changes on the remote board pull down to the local UI
- The **AI agent can interact with the board** directly through this UI as well
