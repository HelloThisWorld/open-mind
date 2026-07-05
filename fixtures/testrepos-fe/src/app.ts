import { formatDate } from "./util.js";

interface User {
  id: string;
  name: string;
  joinedAt: string;
}

export class Dashboard {
  private users: User[] = [];

  async loadUsers(): Promise<void> {
    const response = await fetch("/api/users");
    if (!response.ok) {
      throw new Error(`failed to load users: ${response.status}`);
    }
    this.users = await response.json();
    this.render();
  }

  render(): void {
    const rows = this.users
      .map((u) => `<tr><td>${u.name}</td><td>${formatDate(u.joinedAt)}</td></tr>`)
      .join("");
    const table = document.querySelector("#users");
    if (table) {
      table.innerHTML = rows;
    }
  }
}

new Dashboard().loadUsers();
