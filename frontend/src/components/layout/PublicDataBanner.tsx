import { useAuthStore } from '@/stores/authStore'

/** The broker plugin an install uses when it runs without a broker. */
export const PUBLIC_DATA_BROKER = 'publicdata'

/**
 * A quiet, persistent note on installs that run without a broker: every price
 * is an official end-of-day close and every order goes to the sandbox.
 */
export function PublicDataBanner() {
  const { user } = useAuthStore()
  if (user?.broker !== PUBLIC_DATA_BROKER) return null

  return (
    <div className="border-b border-border bg-muted px-4 py-2 text-center text-sm text-muted-foreground">
      Sandbox only. Prices are NSE end-of-day closes from the official daily files, not live prices.
      Connect a broker to trade live.
    </div>
  )
}
