from .EstateSalesNet import ProcessSaleUrl as ProcessEstateSaleNetUrl
from .MaxSold import ProcessSaleUrl as ProcessMaxSoldUrl
from .EstateSalesOrg import ProcessSaleUrl as ProcessEstateSalesOrgUrl

import argparse

def identifySite(url: str, max_images: int | None = None) -> str:
    if "estatesales.net" in url:
        ProcessEstateSaleNetUrl(url, max_images=max_images)
        FilePath = "EstateSaleNetOutput"
    elif "estatesales.org" in url:
        ProcessEstateSalesOrgUrl(url, max_images=max_images)
        FilePath = "EstateSalesOrgOutput"
    elif "maxsold.com" in url:
        ProcessMaxSoldUrl(url, max_images=max_images)
        FilePath = "MaxSoldOutput"
    else:
        raise ValueError(f"Unsupported URL domain: '{url}'. Supported platforms are EstateSales.net, EstateSales.org, and MaxSold.com")
    
    return FilePath

def main():
    parser = argparse.ArgumentParser(description="Download images from EstateSales.net or MaxSold auctions")
    parser.add_argument("url", help="URL of the auction/sale (EstateSales.net or MaxSold)")
    args = parser.parse_args()

    try:
        output_dir = identifySite(args.url)
        print(f"Images downloaded to: {output_dir}")
    except Exception as e:
        print(f"Error: {e}")
        
if __name__ == "__main__":
    main()