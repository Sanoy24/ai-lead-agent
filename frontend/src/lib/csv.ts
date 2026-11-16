/** Parse CSV text into rows of cells. Handles quoted fields, escaped quotes and CRLF. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      if (row.some((c) => c.trim())) rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  row.push(cell);
  if (row.some((c) => c.trim())) rows.push(row);
  return rows;
}

const COLUMNS: Record<string, string> = {
  name: "name",
  "full name": "name",
  email: "email",
  "email address": "email",
  phone: "phone",
  "phone number": "phone",
  mobile: "phone",
  service: "service_interest",
  "service interest": "service_interest",
  service_interest: "service_interest",
  interest: "service_interest",
  note: "note",
  notes: "note",
};

/** Turn CSV rows into contact objects, matching common header names. */
export function rowsToContacts(rows: string[][]): {
  contacts: Record<string, string>[];
  unknownColumns: string[];
} {
  const [header, ...body] = rows;
  if (!header) return { contacts: [], unknownColumns: [] };
  const keys = header.map((h) => COLUMNS[h.trim().toLowerCase()]);
  const unknownColumns = header.filter((_, i) => !keys[i]).map((h) => h.trim());
  const contacts = body.map((cells) => {
    const contact: Record<string, string> = {};
    keys.forEach((key, i) => {
      const value = cells[i]?.trim();
      if (key && value) contact[key] = value;
    });
    return contact;
  });
  return { contacts, unknownColumns };
}
