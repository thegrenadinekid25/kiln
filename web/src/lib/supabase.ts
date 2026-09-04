import { createClient } from '@supabase/supabase-js'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Kiln has no client-side writes by design: the anon role holds SELECT only,
// and everything lives in the dedicated kiln schema.
export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
  db: { schema: 'kiln' },
  auth: { persistSession: false },
})
