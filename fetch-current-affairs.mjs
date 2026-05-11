import { Readability } from "@mozilla/readability";
import { JSDOM } from "jsdom";
import TurndownService from "turndown";
import { readFileSync, writeFileSync, existsSync, renameSync } from "node:fs";

const CURRENTS_API_KEY = process.env.CURRENTS_API_KEY;
if (!CURRENTS_API_KEY) throw new Error("CURRENTS_API_KEY must be set");

const OUTPUT_FILE = "current-affairs.md";
const turndown = new TurndownService({ headingStyle: "atx", bulletListMarker: "-" });

// Remove images entirely — they clutter markdown
turndown.addRule("removeImages", {
  filter: "img",
  replacement: () => "",
});

// Remove links but keep their text
turndown.addRule("stripLinks", {
  filter: "a",
  replacement: (content) => content,
});

// ── Filters ───────────────────────────────────────────────────────────────────

const BLOCKED_TITLE_KEYWORDS = [
  // Sports
  "fifa", "world cup", "cricket", "ipl", "premier league", "tennis", "italian open",
  "wta", "bagel", "nba", "football", "soccer", "basketball", "rugby", "formula 1",
  "f1", "grand prix", "olympic", "wimbledon", "match", "batting", "bowling", "wicket",
  "golf", "badminton", "kabaddi", "wrestling", "chess tournament",

  // Entertainment / celebrity
  "season 5", "season 4", "season 3", "netflix series", "amazon prime", "disney+",
  "bollywood", "box office", "movie review", "film review", "ott release", "trailer",
  "web series", "opens up on", "breaks silence on", "reveals why", "reveals how",
  "bigg boss", "kbc", "reality show", "celeb", "celebrity", "star kids",
  "laughter chefs", "the kapil sharma", "film city",

  // Lifestyle / filler
  "quote of the day", "proverb of the day", "horoscope", "astrology",
  "recipe", "diet tips", "weight loss", "skin care", "hair care",
  "relationship advice", "toxic romance", "job hunter", "hiring chaos",
  "richest tennis", "richest basketball", "gold, silver prices",
];

const JUNK_PHRASES = [
  "find this comment offensive",
  "choose your reason below",
  "report button",
  "subscribe to read",
  "subscribe now",
  "sign in to read",
  "create your account",
  "already a subscriber",
];

// If content starts with these section labels, the article is entertainment/filler
const BLOCKED_CONTENT_HEADERS = ["bollywood", "entertainment", "lifestyle", "sports"];

// Block articles whose URL path contains these segments
const BLOCKED_URL_PATHS = [
  "/bollywood/", "/entertainment/", "/sports/", "/lifestyle/",
  "/cricket/", "/fashion/", "/beauty/", "/television/", "/movies/",
  "/celebrity/", "/gossip/", "/music/",
];

function isRelevantArticle(article) {
  const titleLower = article.title.toLowerCase();
  if (BLOCKED_TITLE_KEYWORDS.some((kw) => titleLower.includes(kw))) return false;
  if (BLOCKED_URL_PATHS.some((path) => article.url.includes(path))) return false;
  return true;
}

function isJunkContent(text) {
  const lower = text.toLowerCase();
  return JUNK_PHRASES.some((phrase) => lower.includes(phrase));
}

function cleanMarkdown(text) {
  return text
    // Remove "Also Read:" callout lines
    .replace(/\*?\*?Also Read:?\*?\*?[^\n]*/gi, "")
    // Remove "Read more:", "Related:", "See also:" lines
    .replace(/\*?\*?(Read more|Related|See also|Must read):?\*?\*?[^\n]*/gi, "")
    // Remove "Show more Show less" fragments
    .replace(/Show more\s*Show less/gi, "")
    // Remove DNA site chrome
    .replace(/Add DNA as a Preferred Source/gi, "")
    .replace(/Find your daily dose of.*?WhatsApp\./gi, "")
    // Remove "TRENDING NOW" section and everything in it
    .replace(/## TRENDING NOW[\s\S]*?(?=\n[^-\s])/i, "")
    // Remove single-word all-caps section headers (INDIA, WORLD, BUSINESS etc.) on their own line
    .replace(/^[A-Z]{2,15}$/gm, "")
    // Remove publication metadata lines
    .replace(/^Published By:.*$/gm, "")
    .replace(/^Published On:.*$/gm, "")
    .replace(/^\- Ends\s*$/gm, "")
    // Remove image caption lines (plain text describing an image, usually ending with source in parens)
    .replace(/^.{10,80}\(image source:[^)]+\)$/gm, "")
    // Remove leftover bare URLs on their own line
    .replace(/^https?:\/\/\S+$/gm, "")
    // Remove lines that are just dashes or underscores
    .replace(/^[-_]{3,}$/gm, "")
    // Collapse 3+ blank lines into 2
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function hasBlockedContentHeader(text) {
  const firstLine = text.split("\n").find((l) => l.trim().length > 0) ?? "";
  return BLOCKED_CONTENT_HEADERS.some((h) => firstLine.trim().toLowerCase() === h);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

async function fetchArticles() {
  const url = `https://api.currentsapi.services/v1/latest-news?country=IN&language=en&apiKey=${CURRENTS_API_KEY}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Currents API error: HTTP ${res.status}`);
  const json = await res.json();
  if (json.status !== "ok") throw new Error(`Currents API: ${json.message ?? json.status}`);
  return json.news ?? [];
}

async function extractContent(url) {
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; JoeyNewsBot/1.0)" },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) return null;
    const html = await res.text();
    const dom = new JSDOM(html, { url });
    const article = new Readability(dom.window.document).parse();
    if (!article?.content) return null;
    const markdown = cleanMarkdown(turndown.turndown(article.content));
    if (isJunkContent(markdown)) return null;
    if (hasBlockedContentHeader(markdown)) return null;
    if (markdown.split(/\s+/).length < 80) return null;
    return markdown;
  } catch {
    return null;
  }
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  });
}

function checkRollover() {
  if (!existsSync(OUTPUT_FILE)) return;
  const content = readFileSync(OUTPUT_FILE, "utf8");
  const dates = [...content.matchAll(/^## (.+)$/gm)].map((m) => new Date(m[1]));
  if (dates.length === 0) return;
  const oldest = new Date(Math.min(...dates.map((d) => d.getTime())));
  const ageInDays = (Date.now() - oldest.getTime()) / (1000 * 60 * 60 * 24);
  if (ageInDays >= 365) {
    const archiveName = `current-affairs-${oldest.getFullYear()}.md`;
    renameSync(OUTPUT_FILE, archiveName);
    console.log(`Rolled over: archived to ${archiveName}`);
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function run() {
  checkRollover();

  console.log("Fetching articles from Currents API...");
  const allArticles = await fetchArticles();
  console.log(`Got ${allArticles.length} articles`);

  const articles = allArticles.filter(isRelevantArticle);
  console.log(`${articles.length} articles after filter`);

  const today = formatDate(new Date().toISOString());
  const sections = [];

  for (const article of articles) {
    console.log(`Processing: ${article.title}`);
    const content = await extractContent(article.url);

    const category = (article.category ?? []).join(", ");
    // Clean author field — take only what's before any "·" or "SECTIONS" prefix
    const rawAuthor = article.author ?? "";
    const source = rawAuthor.replace(/^SECTIONS\s+.+?(AFP|PTI|ANI|Reuters)\s*/i, "$1")
      .split(/[;·]/)[0].trim() || "Unknown";

    let section = `### ${article.title}\n`;
    section += `*${source} · ${category}*\n\n`;

    if (content) {
      section += `${content}\n`;
    } else {
      section += `${article.description ?? ""}\n`;
      section += `\n[Read full article](${article.url})\n`;
    }

    sections.push(section);
  }

  if (sections.length === 0) {
    console.log("No relevant articles found today, skipping write.");
    return;
  }

  const todayBlock = `## ${today}\n\n${sections.join("\n---\n\n")}`;
  const existing = existsSync(OUTPUT_FILE) ? readFileSync(OUTPUT_FILE, "utf8") : "";
  writeFileSync(OUTPUT_FILE, `${todayBlock}\n\n---\n\n${existing}`.trimEnd() + "\n");

  console.log(`✓ Written ${sections.length} articles to ${OUTPUT_FILE}`);
}

run().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
