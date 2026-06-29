from .EstateSalesNet import ProcessSaleUrl as ProcessEstateSaleNetUrl
from .MaxSold import ProcessSaleUrl as ProcessMaxSoldUrl
from .EstateSalesOrg import ProcessSaleUrl as ProcessEstateSalesOrgUrl

import argparse

def identifySite(url: str) -> str:
    if "estatesales.net" in url:
        ProcessEstateSaleNetUrl(url)
        FilePath = "EstateSaleNetOutput"
    elif "estatesales.org" in url:
        ProcessEstateSalesOrgUrl(url)
        FilePath = "EstateSalesOrgOutput"
    elif "maxsold.com" in url:
        ProcessMaxSoldUrl(url)
        FilePath = "MaxSoldOutput"
    else:
        raise ValueError("Unsupported URL. Please provide a valid EstateSales.net or MaxSold URL.")
    
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