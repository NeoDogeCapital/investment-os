// Public site configuration. The anon (publishable) key belongs here by
// design — it grants nothing without an authenticated, allowlisted session.
// The service key must NEVER appear in this repo; the deploy job refuses to
// publish if it detects one.
window.IWP_CONFIG = {
  supabaseUrl: "REPLACE_WITH_SUPABASE_PROJECT_URL",   // https://<ref>.supabase.co
  supabaseAnonKey: "REPLACE_WITH_PUBLISHABLE_KEY",
  build: "dev-local"                                   // deploy job stamps this
};
