-- Уже применено в проекте Supabase xmrxkuqareicdslioxzo (миграция tg_bot_memory_and_reminders).
-- Таблицы закрыты RLS: доступ только с секретным ключом service_role (он нужен боту).
create table if not exists public.tg_messages (
  id bigserial primary key, owner_id bigint not null, kind text not null default 'text',
  content text, created_at timestamptz not null default now());
create table if not exists public.tg_memories (
  id bigserial primary key, owner_id bigint not null, content text not null,
  created_at timestamptz not null default now());
create table if not exists public.tg_reminders (
  id bigserial primary key, owner_id bigint not null, text text not null,
  event_date date not null, fire_at timestamptz not null, sent boolean not null default false,
  created_at timestamptz not null default now());
create index if not exists tg_reminders_due on public.tg_reminders (sent, fire_at);
alter table public.tg_messages enable row level security;
alter table public.tg_memories enable row level security;
alter table public.tg_reminders enable row level security;
