import { Footer } from "@/components/Footer";
import { MissingPage } from "@/components/MissingPage";
import { getRows } from "@/db/queries";
import { trendingOrder } from "@/lib/feed";

export default async function NotFound() {
  const top = trendingOrder(await getRows()).slice(0, 3).map((r) => ({ slug: r.slug, name: r.name }));
  return (
    <main className="col" id="main" tabIndex={-1}>
      <MissingPage top={top} />
      <Footer />
    </main>
  );
}
