// Pengu fork: Settings → volume categories (traffic baskets). The owner chooses which groups make up each
// basket; each admin's volume per basket is set in the admin dialog.
import { BasketDefinitionsPanel } from '@/features/admins/components/pengu-admin-baskets'

export default function BasketSettings() {
  return (
    <div className="p-4 sm:py-6 lg:py-8">
      <BasketDefinitionsPanel />
    </div>
  )
}
