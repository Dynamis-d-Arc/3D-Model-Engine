#include "TwinHUD.h"

#include "Dom/JsonObject.h"
#include "Engine/Canvas.h"
#include "Engine/Engine.h"
#include "Engine/Font.h"
#include "TwinAirflowActor.h"
#include "TwinMeta.h"
#include "TwinPlayerController.h"
#include "TwinViewerDirector.h"

namespace
{
	const FLinearColor Blue(0.2f, 0.45f, 0.75f, 1.f);
	const FLinearColor Amber(1.f, 0.72f, 0.12f, 1.f);
	const FLinearColor Red(0.95f, 0.3f, 0.25f, 1.f);
	const FLinearColor Green(0.3f, 0.8f, 0.45f, 1.f);
	const FLinearColor TextMain(0.85f, 0.87f, 0.9f);
	const FLinearColor TextDim(0.64f, 0.68f, 0.74f);
	const FLinearColor TextHeading(0.56f, 0.76f, 1.f);
	const FLinearColor LiveText(0.6f, 0.93f, 0.66f);
	const FLinearColor SimText(1.f, 0.82f, 0.45f);
	const FLinearColor WarnText(1.f, 0.84f, 0.58f);
	const FLinearColor ProblemText(1.f, 0.66f, 0.62f);
	const FLinearColor PanelBg(0.02f, 0.03f, 0.05f, 0.62f);
	const TCHAR* Deg = TEXT("°C");
	const TCHAR* Sep = TEXT(" · ");

	constexpr float Margin = 18.f;      // panels keep this far from the window edges
	constexpr float LineH = 22.f;
	constexpr float TextLeft = 14.f;    // accent bar + padding
	constexpr float PadRight = 14.f;
	constexpr float BadgeSize = 16.f;
	constexpr float BadgeW = 22.f;      // badge + space before the text
	constexpr float KeyH = 20.f;
	constexpr float KeyScale = 0.78f;
	constexpr float HelpTextScale = 0.9f;

	FString Temp(float C)
	{
		return FMath::IsNaN(C) ? FString(TEXT("-")) : FString::Printf(TEXT("%.1f %s"), C, Deg);
	}

	/** Setpoints: "24 °C", "24.5 °C". */
	FString TempShort(float C)
	{
		if (FMath::IsNaN(C))
		{
			return TEXT("-");
		}
		return FMath::IsNearlyEqual(C, FMath::RoundToFloat(C), 0.05f) ? FString::Printf(TEXT("%.0f %s"), C, Deg) : Temp(C);
	}

	FString Ago(double Seconds)
	{
		Seconds = FMath::Max(0.0, Seconds);
		if (Seconds < 90.0)
		{
			return FString::Printf(TEXT("%.0f s"), Seconds);
		}
		return Seconds < 5400.0 ? FString::Printf(TEXT("%.0f min"), Seconds / 60.0) : FString::Printf(TEXT("%.1f h"), Seconds / 3600.0);
	}

	FTwinHudLine TitleLine(const FString& Text)
	{
		return FTwinHudLine(Text, FLinearColor::White, 1.05f);
	}

	FTwinHudLine DimLine(const FString& Text)
	{
		return FTwinHudLine(Text, TextDim, 0.88f);
	}

	FTwinHudLine BadgeLine(const FString& Text, const FLinearColor& Badge, const FLinearColor& Color, const TCHAR* Glyph = TEXT("!"))
	{
		FTwinHudLine L(Text, Color);
		L.Badge = Badge;
		L.BadgeGlyph = Glyph;
		return L;
	}

	FTwinHudLine SectionLine(const FString& Text)
	{
		FTwinHudLine L(Text, TextHeading, 0.8f);
		L.Gap = 8.f;
		L.bRule = true;
		return L;
	}

	/** "oak_planks" -> "Oak planks". */
	FString Readable(const FString& Id)
	{
		FString S = Id.Replace(TEXT("_"), TEXT(" "));
		S.TrimStartAndEndInline();
		while (S.ReplaceInline(TEXT("  "), TEXT(" ")) > 0)
		{
		}
		if (S.Len() > 0)
		{
			S[0] = FChar::ToUpper(S[0]);
		}
		return S;
	}

	/** "6.25 x 4.25 m" -> "6.25 × 4.25 m". */
	FString PrettySize(const FString& S)
	{
		return S.Replace(TEXT(" x "), TEXT(" × "));
	}

	/** "26.6 m2" -> "26.6 m²". */
	FString PrettyArea(const FString& S)
	{
		return S.EndsWith(TEXT(" m2")) ? S.LeftChop(1) + TEXT("²") : S;
	}

	/** The flooring's display name: a name in the metadata when there is one, else the material id made readable. */
	FString FloorFinish(const ATwinViewerDirector* D, const AActor* Floor, const FString& Id)
	{
		const FString Named = TwinMeta::Get(Floor, TEXT("FloorMaterialName"));
		if (!Named.IsEmpty())
		{
			return Named;
		}
		const TSharedPtr<FJsonObject>* Mats = nullptr;
		const TSharedPtr<FJsonObject>* Mat = nullptr;
		FString Name;
		if (D && D->Manifest.IsValid() && D->Manifest->TryGetObjectField(TEXT("materials"), Mats) && (*Mats)->TryGetObjectField(Id, Mat) &&
			(*Mat)->TryGetStringField(TEXT("name"), Name) && !Name.IsEmpty())
		{
			return Name;
		}
		return Readable(Id);
	}

	/** What a selected object is, in words a building owner uses. */
	FString FriendlyKind(const AActor* A)
	{
		const FString Kind = TwinMeta::Get(A, TEXT("Kind"));
		if (Kind == TEXT("hvac_unit"))
		{
			const FString U = TwinMeta::Get(A, TEXT("UnitKind"));
			if (U == TEXT("fcu_ducted"))
			{
				return TEXT("Air-conditioning unit (ducted, in the ceiling)");
			}
			if (U == TEXT("fcu_cassette"))
			{
				return TEXT("Air-conditioning unit (ceiling cassette)");
			}
			if (U == TEXT("fcu_exposed"))
			{
				return TEXT("Air-conditioning unit (exposed)");
			}
			if (U == TEXT("split_wall"))
			{
				return TEXT("Air-conditioning unit (wall-mounted split)");
			}
			if (U == TEXT("ahu"))
			{
				return TEXT("Air-handling unit");
			}
			if (U == TEXT("exhaust_fan"))
			{
				return TEXT("Exhaust fan");
			}
			return TEXT("Air-conditioning unit");
		}
		if (Kind == TEXT("hvac_duct") || Kind == TEXT("duct"))
		{
			const FString Sys = TwinMeta::Get(A, TEXT("System"));
			return Sys == TEXT("supply")      ? TEXT("Supply-air duct")
			     : Sys == TEXT("return")      ? TEXT("Return-air duct")
			     : Sys == TEXT("exhaust")     ? TEXT("Exhaust duct")
			     : Sys == TEXT("fresh_air")   ? TEXT("Fresh-air duct")
			                                  : TEXT("Duct");
		}
		if (Kind == TEXT("hvac_grille"))
		{
			const FString Label = TwinMeta::Get(A, TEXT("Label"));
			return Label.StartsWith(TEXT("Return"))  ? TEXT("Air intake (return grille)")
			     : Label.StartsWith(TEXT("Exhaust")) ? TEXT("Air extract (exhaust grille)")
			                                         : TEXT("Air outlet (grille)");
		}
		static const TCHAR* const Names[][2] = {
			{TEXT("hvac_diffuser"), TEXT("Air outlet (diffuser)")},
			{TEXT("hvac_terminal"), TEXT("Air outlet / grille")},
			{TEXT("furniture"), TEXT("Furniture")},
			{TEXT("sensor"), TEXT("Sensor")},
			{TEXT("floor"), TEXT("Room floor")},
			{TEXT("room"), TEXT("Room")},
			{TEXT("wall"), TEXT("Wall")},
			{TEXT("ceiling"), TEXT("Ceiling")},
			{TEXT("door_leaf"), TEXT("Door")},
			{TEXT("door_frame"), TEXT("Door frame")},
			{TEXT("window_frame"), TEXT("Window")},
			{TEXT("glass"), TEXT("Window glass")},
			{TEXT("opening"), TEXT("Opening")},
			{TEXT("slab"), TEXT("Floor slab")},
			{TEXT("project"), TEXT("Building")},
		};
		for (const auto& N : Names)
		{
			if (Kind == N[0])
			{
				return N[1];
			}
		}
		return (Kind.IsEmpty() || Kind == TEXT("unknown")) ? FString(TEXT("Part of the building")) : Readable(Kind);
	}

	/** BMS mode -> "cooling", "heating", ...; nothing for on / off. */
	FString ModeWord(const FString& Mode)
	{
		const FString M = Mode.ToLower();
		if (M.IsEmpty() || M == TEXT("off") || M == TEXT("on"))
		{
			return FString();
		}
		if (M.StartsWith(TEXT("cool")))
		{
			return TEXT("cooling");
		}
		if (M.StartsWith(TEXT("heat")))
		{
			return TEXT("heating");
		}
		if (M.StartsWith(TEXT("fan")))
		{
			return TEXT("fan only");
		}
		return M == TEXT("dry") ? FString(TEXT("drying")) : M.Replace(TEXT("_"), TEXT(" "));
	}

	/**
	 * Where this object's reading comes from: live | simulated | unavailable, or for a multi-floor building whose
	 * floors differ ("mixed") the source of the object's own floor, read from the feed's per-floor label.
	 */
	FString SourceOf(const ATwinViewerDirector* D, const AActor* Sel)
	{
		const FTwinTelemetry& T = D->Telemetry;
		if (T.Source != TEXT("mixed"))
		{
			return T.Source;
		}
		const int32 F = D->FloorIndexOf(TwinMeta::Get(Sel, TEXT("Floor")));
		if (D->Floors.IsValidIndex(F))
		{
			TArray<FString> Parts;
			T.Label.ParseIntoArray(Parts, TEXT("; "));
			for (const FString& P : Parts)
			{
				FString Floor, Label;
				if (P.Split(TEXT(": "), &Floor, &Label) && Floor == D->Floors[F].Label)
				{
					if (Label.Contains(TEXT("not measured")))
					{
						return TEXT("simulated");
					}
					if (Label.Contains(TEXT("(measured)")))
					{
						return TEXT("live");
					}
				}
			}
		}
		return TEXT("mixed");
	}

	bool HasAirflow(const ATwinViewerDirector* D)
	{
		return D && D->AirflowActor && D->AirflowActor->NumDots() > 0;
	}
}

// ------------------------------------------------------------------------------------------------
// panels and key caps
// ------------------------------------------------------------------------------------------------

FVector2D ATwinHUD::LayoutPanel(TArray<FTwinHudLine>& Lines, float MinW, float MaxW)
{
	UFont* Font = GEngine->GetMediumFont();
	MaxW = FMath::Max(MaxW, MinW);
	const float Room = MaxW - TextLeft - PadRight;
	TArray<FTwinHudLine> Out;
	float Widest = 0.f, H = 14.f;
	for (const FTwinHudLine& In : Lines)
	{
		const float Lead = In.Badge.A > 0.f ? BadgeW : In.Indent;
		FString Rest = In.Text;
		bool bFirst = true;
		while (true)
		{
			// word wrap: the longest run of words that fits (one over-long word runs over)
			FString Piece = Rest;
			float W = 0.f, TH = 0.f;
			GetTextSize(Piece, W, TH, Font, In.Scale);
			if (Lead + W > Room)
			{
				for (int32 i = Rest.Len() - 1; i > 0; --i)
				{
					if (Rest[i] != TEXT(' '))
					{
						continue;
					}
					float CW = 0.f;
					GetTextSize(Rest.Left(i), CW, TH, Font, In.Scale);
					if (Lead + CW <= Room)
					{
						Piece = Rest.Left(i).TrimEnd();
						W = CW;
						break;
					}
				}
			}
			FTwinHudLine O = In;
			O.Text = Piece;
			if (!bFirst)
			{
				O.Badge = FLinearColor::Transparent;
				O.BadgeGlyph.Empty();
				O.Gap = 0.f;
				O.bRule = false;
				O.Indent = Lead;
			}
			Widest = FMath::Max(Widest, Lead + W);
			H += LineH + O.Gap;
			Out.Add(MoveTemp(O));
			bFirst = false;
			if (Piece.Len() >= Rest.Len())
			{
				break;
			}
			Rest = Rest.RightChop(Piece.Len()).TrimStart();
		}
	}
	Lines = MoveTemp(Out);
	return FVector2D(FMath::Clamp(TextLeft + Widest + PadRight, MinW, MaxW), H);
}

FBox2D ATwinHUD::DrawPanel(float X, float Y, const FVector2D& Size, const TArray<FTwinHudLine>& Lines, const FLinearColor& Accent)
{
	UFont* Font = GEngine->GetMediumFont();
	DrawRect(PanelBg, X, Y, Size.X, Size.Y);
	DrawRect(Accent, X, Y, 4.f, Size.Y);
	float LY = Y + 8.f;
	for (const FTwinHudLine& L : Lines)
	{
		if (L.bRule)
		{
			DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.14f), X + TextLeft, LY + L.Gap * 0.5f - 1.f, Size.X - TextLeft - PadRight, 1.f);
		}
		LY += L.Gap;
		float TX = X + TextLeft + L.Indent;
		if (L.Badge.A > 0.f)
		{
			const float BY = LY + 2.f;
			DrawRect(L.Badge, TX, BY, BadgeSize, BadgeSize);
			if (!L.BadgeGlyph.IsEmpty())
			{
				float GW = 0.f, GH = 0.f;
				GetTextSize(L.BadgeGlyph, GW, GH, Font, 0.8f);
				const FLinearColor Ink = L.Badge.GetLuminance() > 0.45f ? FLinearColor(0.05f, 0.05f, 0.06f) : FLinearColor::White;
				DrawText(L.BadgeGlyph, Ink, TX + (BadgeSize - GW) * 0.5f, BY + (BadgeSize - GH) * 0.5f, Font, 0.8f);
			}
			TX += BadgeW;
		}
		DrawText(L.Text, L.Color, TX, LY, Font, L.Scale);
		LY += LineH;
	}
	const FBox2D R(FVector2D(X, Y), FVector2D(X + Size.X, Y + Size.Y));
	Used.Add(R);
	return R;
}

float ATwinHUD::Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent)
{
	TArray<FTwinHudLine> L;
	for (int32 i = 0; i < Lines.Num(); ++i)
	{
		L.Add(i == 0 ? TitleLine(Lines[i]) : FTwinHudLine(Lines[i]));
	}
	const FVector2D S = LayoutPanel(L, W, W);
	DrawPanel(X, Y, S, L, Accent);
	return (float)S.Y;
}

float ATwinHUD::KeyCap(float X, float Y, const FString& Label, bool bDraw)
{
	UFont* Font = GEngine->GetMediumFont();
	float TW = 0.f, TH = 0.f;
	GetTextSize(Label, TW, TH, Font, KeyScale);
	const float W = FMath::Max(KeyH, TW + 12.f);
	if (bDraw)
	{
		DrawRect(FLinearColor(0.85f, 0.9f, 1.f, 0.17f), X, Y, W, KeyH);
		DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.4f), X, Y, W, 1.f);             // top edge
		DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.22f), X, Y, 1.f, KeyH);          // sides
		DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.22f), X + W - 1.f, Y, 1.f, KeyH);
		DrawRect(FLinearColor(0.f, 0.f, 0.f, 0.5f), X, Y + KeyH - 2.f, W, 2.f);  // bottom shadow: reads as a key
		DrawText(Label, FLinearColor::White, X + (W - TW) * 0.5f, Y + (KeyH - 2.f - TH) * 0.5f, Font, KeyScale);
	}
	return W;
}

float ATwinHUD::Keys(float X, float Y, const FString& Spec, bool bDraw)
{
	UFont* Font = GEngine->GetMediumFont();
	TArray<FString> Parts;
	Spec.ParseIntoArray(Parts, TEXT("|"));
	float W = 0.f;
	for (int32 i = 0; i < Parts.Num(); ++i)
	{
		W += i > 0 ? 3.f : 0.f;
		const FString& P = Parts[i];
		if (P == TEXT("/") || P == TEXT("+") || P == TEXT("or"))
		{
			float TW = 0.f, TH = 0.f;
			GetTextSize(P, TW, TH, Font, KeyScale);
			if (bDraw)
			{
				DrawText(P, TextDim, X + W + 1.f, Y + (KeyH - 2.f - TH) * 0.5f, Font, KeyScale);
			}
			W += TW + 2.f;
		}
		else
		{
			W += KeyCap(X + W, Y, P, bDraw);
		}
	}
	return W;
}

void ATwinHUD::DrawBox(const FBox& Box, const FLinearColor& Color)
{
	FVector C[8];
	for (int32 i = 0; i < 8; ++i)
	{
		const FVector P((i & 1) ? Box.Max.X : Box.Min.X, (i & 2) ? Box.Max.Y : Box.Min.Y, (i & 4) ? Box.Max.Z : Box.Min.Z);
		C[i] = Project(P, false);
		if (C[i].Z <= 0.f)
		{
			return;  // behind the camera
		}
	}
	static const int32 Edges[12][2] = {{0, 1}, {1, 3}, {3, 2}, {2, 0}, {4, 5}, {5, 7}, {7, 6}, {6, 4}, {0, 4}, {1, 5}, {2, 6}, {3, 7}};
	for (const auto& E : Edges)
	{
		DrawLine(C[E[0]].X, C[E[0]].Y, C[E[1]].X, C[E[1]].Y, Color, 2.f);
	}
}

// ------------------------------------------------------------------------------------------------
// loading screen and menus
// ------------------------------------------------------------------------------------------------

void ATwinHUD::DrawLoadScreen(ATwinPlayerController* PC, ATwinViewerDirector* D)
{
	DrawRect(FLinearColor(0.03f, 0.04f, 0.06f, 1.f), 0, 0, Canvas->ClipX, Canvas->ClipY);
	const float W = FMath::Min(760.f, Canvas->ClipX - 40.f);
	const float X = (Canvas->ClipX - W) * 0.5f;
	float Y = Canvas->ClipY * 0.3f;
	const FString Title = D->Launch.ProjectName.IsEmpty() ? FString(TEXT("Floor Plan Twin viewer")) : D->Launch.ProjectName;
	DrawText(Title, FLinearColor::White, X, Y - 44.f, GEngine->GetLargeFont(), 1.0f);
	TArray<FString> Lines;
	FLinearColor Accent = Blue;
	if (D->State == ETwinLoadState::Error)
	{
		Accent = Red;
		Lines.Add(FString::Printf(TEXT("Could not open the building  (%s)"), *D->ErrorCode));
		Lines.Add(D->ErrorMessage);   // wrapped by the panel
		Lines.Add(TEXT(""));
		if (D->ErrorCode.StartsWith(TEXT("TOKEN_")))
		{
			Lines.Add(TEXT("The viewer link is not valid any more: open the building again from the web app."));
		}
		Lines.Add(PC->bStreaming ? TEXT("R retry  |  B choose another building")
		                         : TEXT("R retry  |  B choose another building  |  Ctrl+V paste a new launch code  |  Esc quit"));
	}
	else if (D->State == ETwinLoadState::Idle && PC->bStreaming)
	{
		Lines.Add(TEXT("Waiting for the building of this session..."));
	}
	else if (D->State == ETwinLoadState::Idle)
	{
		Lines.Add(TEXT("No building selected"));
		Lines.Add(TEXT("In the web app, open a generated building and choose 'Open in desktop viewer'."));
		Lines.Add(TEXT("Start the viewer with the downloaded .twinview file (Open Twin Viewer.cmd <file>),"));
		Lines.Add(TEXT("or copy the launch code there and press Ctrl+V here.   Esc quits."));
	}
	else
	{
		Lines.Add(D->Stage);
		if (D->State == ETwinLoadState::Downloading && D->BytesTotal > 0)
		{
			Lines.Add(FString::Printf(TEXT("%.1f of %.1f MB"), D->BytesDone / 1048576.0, D->BytesTotal / 1048576.0));
		}
		else if (D->bFromCache)
		{
			Lines.Add(FString::Printf(TEXT("Model from the local cache (%.1f MB)"), D->BytesTotal / 1048576.0));
		}
		Lines.Add(FString::Printf(TEXT("Server %s"), *D->Launch.Server));
	}
	const float H = Panel(X, Y, W, Lines, Accent);
	if (D->IsBusy())
	{
		const float BarY = Y + H + 14.f;
		DrawRect(FLinearColor(1, 1, 1, 0.12f), X, BarY, W, 10.f);
		DrawRect(Blue, X, BarY, W * FMath::Clamp(D->Progress, 0.f, 1.f), 10.f);
	}
	if (!PC->Status.IsEmpty() && GetWorld()->GetTimeSeconds() < PC->StatusUntil)
	{
		Panel(X, Canvas->ClipY - 70.f, W, {PC->Status}, Blue);
	}
}

void ATwinHUD::DrawMenu(ATwinPlayerController* PC, ATwinViewerDirector* D)
{
	PC->MenuItems.Reset();
	const float W = FMath::Min(1000.f, Canvas->ClipX - 60.f);
	const float X = (Canvas->ClipX - W) * 0.5f, Y = 90.f;
	const float RowH = 26.f;
	const int32 Rows = FMath::Max(4, (int32)((Canvas->ClipY - Y - 120.f) / RowH));
	float MX = 0.f, MY = 0.f;
	PC->GetMousePosition(MX, MY);
	TArray<FString> Items;
	TArray<bool> Current;
	FString Title, Footer;
	int32 Cols = 1;
	if (PC->Menu == ATwinPlayerController::EMenu::Rooms)
	{
		Cols = 2;
		Title = FString::Printf(TEXT("Rooms (%d)  -  click a room to go there"), PC->RoomFloors.Num());
		for (const auto& F : PC->RoomFloors)
		{
			Items.Add(F.IsValid() ? FString::Printf(TEXT("%s  %s  (%s)"), *TwinMeta::Get(F.Get(), TEXT("Room")),
			                                        *TwinMeta::Get(F.Get(), TEXT("RoomName")), *PrettyArea(TwinMeta::Get(F.Get(), TEXT("Area"))))
			                      : FString());
			Current.Add(F == PC->CurrentRoomFloor);
		}
		Footer = TEXT("M / Esc close  |  PgUp / PgDn page");
	}
	else
	{
		Title = TEXT("Buildings you can open  -  click to load");
		if (D)
		{
			for (const FTwinBuildingEntry& B : D->Buildings)
			{
				Items.Add(FString::Printf(TEXT("%s   v%d  |  %d rooms  |  %d items  |  %.1f MB  |  %s"), *B.Name, B.Version, B.Rooms,
				                          B.Furniture, B.Bytes / 1048576.0, *B.BuildId));
				Current.Add(B.BuildId == D->Launch.BuildId && B.ProjectId == D->Launch.ProjectId);
			}
			Footer = D->bBuildingsLoading ? TEXT("Loading the list...") : (!D->BuildingsError.IsEmpty() ? D->BuildingsError
			                                : TEXT("B / Esc close  |  R refresh  |  PgUp / PgDn page"));
		}
	}
	const int32 PerPage = Rows * Cols;
	const int32 Pages = FMath::Max(1, FMath::DivideAndRoundUp(Items.Num(), PerPage));
	PC->MenuPage = FMath::Clamp(PC->MenuPage, 0, Pages - 1);
	const int32 First = PC->MenuPage * PerPage;
	const int32 Shown = FMath::Min(PerPage, Items.Num() - First);
	const int32 UsedRows = FMath::Min(Rows, FMath::DivideAndRoundUp(FMath::Max(Shown, 1), Cols));
	DrawRect(FLinearColor(0.02f, 0.03f, 0.05f, 0.88f), X - 10.f, Y - 46.f, W + 20.f, UsedRows * RowH + 92.f);
	DrawText(FString::Printf(TEXT("%s   (page %d/%d)"), *Title, PC->MenuPage + 1, Pages), FLinearColor::White, X, Y - 36.f,
	         GEngine->GetMediumFont(), 1.05f);
	const float ColW = W / Cols;
	for (int32 k = 0; k < Shown; ++k)
	{
		const int32 Index = First + k;
		const float IX = X + (k / Rows) * ColW, IY = Y + (k % Rows) * RowH;
		const FBox2D Rect(FVector2D(IX, IY), FVector2D(IX + ColW - 8.f, IY + RowH - 2.f));
		const bool bHover = Rect.IsInside(FVector2D(MX, MY));
		DrawRect(Current[Index] ? FLinearColor(0.2f, 0.45f, 0.75f, 0.45f) : FLinearColor(1, 1, 1, bHover ? 0.16f : 0.05f),
		         Rect.Min.X, Rect.Min.Y, Rect.GetSize().X, Rect.GetSize().Y);
		DrawText(Items[Index], FLinearColor::White, IX + 8.f, IY + 3.f, GEngine->GetMediumFont(), 0.9f);
		ATwinPlayerController::FMenuItem& Item = PC->MenuItems.AddDefaulted_GetRef();
		Item.Rect = Rect;
		Item.Index = Index;
	}
	if (Items.Num() == 0)
	{
		DrawText(TEXT("(nothing to show)"), FLinearColor(0.8f, 0.8f, 0.8f), X, Y, GEngine->GetMediumFont());
	}
	DrawText(Footer, FLinearColor(0.8f, 0.82f, 0.86f), X, Y + UsedRows * RowH + 14.f, GEngine->GetSmallFont());
}

// ------------------------------------------------------------------------------------------------
// walkthrough panels
// ------------------------------------------------------------------------------------------------

FBox2D ATwinHUD::DrawInfoPanel(ATwinPlayerController* PC, ATwinViewerDirector* D)
{
	TArray<FTwinHudLine> L;
	TArray<FTwinHudLine> RoomDetails;
	const AActor* Info = PC->ProjectInfo.Get();
	const FString Name = Info ? TwinMeta::Get(Info, TEXT("Name")) : FString();
	L.Add(TitleLine(Name.IsEmpty() ? FString(TEXT("Floor Plan Twin")) : Name));
	if (const AActor* Floor = PC->CurrentRoomFloor.Get())
	{
		FString RoomName = TwinMeta::Get(Floor, TEXT("RoomName"));
		if (D && D->IsMultiFloor() && D->Floors.IsValidIndex(PC->CurrentFloor))
		{
			// stacked buildings name rooms "Level 2 · Kitchen"; the storey has its own line below
			const FString Prefix = D->Floors[PC->CurrentFloor].Label + Sep;
			if (RoomName.StartsWith(Prefix))
			{
				RoomName.RightChopInline(Prefix.Len());
			}
		}
		L.Add(FTwinHudLine(RoomName.IsEmpty() ? TwinMeta::Get(Floor, TEXT("Room")) : RoomName, FLinearColor::White, 1.f));
		TArray<FString> Size;
		const FString Dims = TwinMeta::Get(Floor, TEXT("Dims"));
		const FString Area = TwinMeta::Get(Floor, TEXT("Area"));
		if (!Dims.IsEmpty())
		{
			Size.Add(PrettySize(Dims));
		}
		if (!Area.IsEmpty())
		{
			Size.Add(PrettyArea(Area));
		}
		if (Size.Num())
		{
			L.Add(FString::Join(Size, Sep));
		}
		const FString FinishId = TwinMeta::Get(Floor, TEXT("FloorMaterial"));
		if (!FinishId.IsEmpty())
		{
			L.Add(FString::Printf(TEXT("Flooring: %s"), *FloorFinish(D, Floor, FinishId)));
		}
		RoomDetails.Add(DimLine(FString::Printf(TEXT("Room id %s%s"), *TwinMeta::Get(Floor, TEXT("Room")),
		                                        FinishId.IsEmpty() ? TEXT("") : *(FString(TEXT(" · flooring id ")) + FinishId))));
	}
	else
	{
		L.Add(FTwinHudLine(TEXT("Outside the rooms"), TextDim));
	}
	if (D && D->IsMultiFloor() && D->Floors.IsValidIndex(PC->CurrentFloor))
	{
		L.Add(FString::Printf(TEXT("%s  (floor %d of %d)"), *D->Floors[PC->CurrentFloor].Label, PC->CurrentFloor + 1, D->Floors.Num()));
	}
	L.Add(FString::Printf(TEXT("Mode: %s"), *PC->ModeName()));

	FLinearColor Accent = Blue;
	if (D)
	{
		// short plain warnings; the numbers behind them are in the details (I)
		if (D->ValidationFailures > 0)
		{
			Accent = Red;
			L.Add(BadgeLine(D->ValidationFailures == 1 ? FString(TEXT("1 object is not where the plan says (press I)"))
			                                           : FString::Printf(TEXT("%d objects are not where the plan says (press I)"), D->ValidationFailures),
			                Red, ProblemText));
		}
		const FTwinTelemetry& T = D->Telemetry;
		if (T.BuildUnits > 0)
		{
			const double Age = T.ReceivedAt > 0.0 ? FPlatformTime::Seconds() - T.ReceivedAt : 0.0;
			FString Warn;
			if (T.Failures > 0)
			{
				Warn = T.Source.IsEmpty() ? TEXT("Cannot reach the air-conditioning data (press I)")
				                          : TEXT("Air-conditioning data interrupted, showing the last reading (press I)");
			}
			else if (T.Source == TEXT("unavailable"))
			{
				Warn = TEXT("Air-conditioning data is not available (press I)");
			}
			else if (!T.Source.IsEmpty() && Age > 30.0)
			{
				Warn = FString::Printf(TEXT("Air-conditioning data not updated for %s (press I)"), *Ago(Age));
			}
			if (!Warn.IsEmpty())
			{
				L.Add(BadgeLine(Warn, Amber, WarnText));
			}
		}
	}

	if (PC->bShowDetails)
	{
		L.Add(SectionLine(TEXT("TECHNICAL DETAILS  (I hides them)")));
		if (D)
		{
			L.Add(DimLine(D->ValidationFailures == 0
				              ? FString::Printf(TEXT("Model check: %d objects match the floor plan (max %.2f cm)"), D->ValidationChecked, D->MaxDeviationCm)
				              : FString::Printf(TEXT("Model check: %d PROBLEMS of %d objects checked (max %.2f cm off), see Saved/TwinViewer/validation_%s.json"),
				                                D->ValidationFailures, D->ValidationChecked, D->MaxDeviationCm, *D->Launch.BuildId)));
			const FTwinTelemetry& T = D->Telemetry;
			if (T.BuildUnits == 0)
			{
				L.Add(DimLine(TEXT("HVAC data: no HVAC layer in this building")));
			}
			else if (T.Source.IsEmpty())
			{
				L.Add(DimLine(TEXT("HVAC data: connecting...")));
			}
			else
			{
				L.Add(DimLine(FString::Printf(TEXT("HVAC data: %s%s%d/%d units, last reply %s ago"), *T.Label, Sep, T.Units.Num(), T.BuildUnits,
				                              *Ago(FPlatformTime::Seconds() - T.ReceivedAt))));
				if (T.Missing > 0 || T.Unknown > 0)
				{
					L.Add(DimLine(FString::Printf(TEXT("HVAC matching: %d units without a reading, %d unknown tags in the feed"), T.Missing, T.Unknown)));
				}
			}
			if (T.Failures > 0 || (T.Source == TEXT("unavailable") && !T.Error.IsEmpty()))
			{
				L.Add(DimLine(FString::Printf(TEXT("HVAC feed problem (%d tries): %s"), T.Failures, *T.Error.Left(160))));
			}
			L.Add(DimLine(FString::Printf(TEXT("Build %s%sproject %s"), *D->Launch.BuildId, Sep, *D->Launch.ProjectId)));
		}
		L.Append(RoomDetails);
	}
	const FVector2D S = LayoutPanel(L, 240.f, FMath::Min(460.f, Canvas->ClipX * 0.4f));
	return DrawPanel(Margin, Margin, S, L, Accent);
}

FBox2D ATwinHUD::DrawAirflowBanner(ATwinViewerDirector* D, int32 Floor, const FBox2D& Info)
{
	TArray<FTwinHudLine> Lines;
	FString Head = TEXT("AIRFLOW ILLUSTRATION  –  schematic, not measured, not a CFD result");
	// a multi-floor building: the floor the visitor is on (each floor has its own illustration and MATLAB zones)
	const ATwinAirflowActor* A = D->AirflowForFloor(Floor);
	if (D->IsMultiFloor() && D->Floors.IsValidIndex(Floor))
	{
		Head += FString::Printf(TEXT("%s%s"), Sep, *D->Floors[Floor].Label);
	}
	Lines.Add(TitleLine(Head));
	if (D->IsMultiFloor() && !A)
	{
		Lines.Add(FTwinHudLine(TEXT("No HVAC layer on this floor")));
	}
	const FString Driver = A ? A->Driver() : FString();
	const FTwinTelemetry& T = D->Telemetry;
	FString Mist = Driver == TEXT("feed")     ? FString::Printf(TEXT("Mist: air temperature + cooling jet of each running unit, from %s"), *T.Label)
	             : Driver == TEXT("matlab")   ? FString(TEXT("Mist: west / east zone temperatures of the MATLAB thermal model (simulation)"))
	             : Driver == TEXT("waiting")  ? FString(TEXT("Mist: waiting for the HVAC feed"))
	                                          : FString(TEXT("Mist: no temperature data (tracers only)"));
	if (A && !A->IsMistShown())
	{
		Mist += TEXT("  (hidden, T shows it)");
	}
	Lines.Add(FTwinHudLine(Mist));
	Lines.Add(FTwinHudLine(TEXT("Tracers: circulation loops from each unit's outlet (drawing positions, not a flow solution)")));
	const TSharedPtr<FJsonObject> Thermal = A ? A->ThermalState() : TSharedPtr<FJsonObject>();
	bool bAvail = false;
	if (Thermal.IsValid() && Thermal->TryGetBoolField(TEXT("available"), bAvail) && bAvail)
	{
		Lines.Add(FTwinHudLine(FString::Printf(TEXT("MATLAB thermal model (simulation): west %s | east %s | setpoint %.0f %s | minute %d/%d"),
		                                       *Temp((float)Thermal->GetNumberField(TEXT("west_c"))), *Temp((float)Thermal->GetNumberField(TEXT("east_c"))),
		                                       Thermal->GetNumberField(TEXT("setpoint_c")), Deg, (int32)Thermal->GetNumberField(TEXT("minute")),
		                                       (int32)Thermal->GetNumberField(TEXT("minutes")))));
	}
	// centred, but never over the room panel on the left; below it when the window is too narrow
	const float X0 = (float)Info.Max.X + 14.f;
	float W = FMath::Min(760.f, Canvas->ClipX - Margin - X0);
	float X = 0.f, Y = Margin;
	if (W >= 420.f)
	{
		X = FMath::Max((Canvas->ClipX - W) * 0.5f, X0);
	}
	else
	{
		W = FMath::Min(760.f, Canvas->ClipX - 2.f * Margin);
		X = (Canvas->ClipX - W) * 0.5f;
		Y = (float)Info.Max.Y + 10.f;
	}
	const FVector2D S = LayoutPanel(Lines, W, W);
	const FBox2D Box = DrawPanel(X, Y, S, Lines, Amber);
	// legend
	UFont* Small = GEngine->GetSmallFont();
	const float LY = (float)Box.Max.Y + 6.f;
	const float Steps[] = {12.f, 16.f, 20.f, 24.f, 28.f, 32.f, 36.f};
	const float SW = W / UE_ARRAY_COUNT(Steps);
	for (int32 i = 0; i < UE_ARRAY_COUNT(Steps); ++i)
	{
		DrawRect(ATwinAirflowActor::HeatColor(Steps[i] + 2.f), X + i * SW, LY, SW, 14.f);
		DrawText(FString::Printf(TEXT("%.0f %s"), Steps[i], Deg), FLinearColor::White, X + i * SW + 4.f, LY + 16.f, Small);
	}
	const FBox2D All(Box.Min, FVector2D(Box.Max.X, LY + 36.f));
	Used.Add(All);
	return All;
}

void ATwinHUD::HvacLines(ATwinViewerDirector* D, const AActor* Sel, TArray<FTwinHudLine>& Lines, TArray<FTwinHudLine>& Details)
{
	const FString Kind = TwinMeta::Get(Sel, TEXT("Kind"));
	if (!Kind.StartsWith(TEXT("hvac_")))
	{
		return;
	}
	const bool bUnit = Kind == TEXT("hvac_unit");
	const FString Tag = TwinMeta::Get(Sel, TEXT("Tag"));
	const FString UnitId = TwinMeta::Get(Sel, TEXT("Unit"));
	if (bUnit)
	{
		if (!Tag.IsEmpty() && !TwinMeta::Get(Sel, TEXT("Label")).Contains(Tag))
		{
			Lines.Add(FString::Printf(TEXT("Unit tag %s"), *Tag));
		}
	}
	else if (!Tag.IsEmpty())
	{
		Lines.Add(FString::Printf(TEXT("Connected to unit %s"), *Tag));
	}
	const FString Airflow = TwinMeta::Get(Sel, TEXT("Airflow"));
	if (!Airflow.IsEmpty())
	{
		Lines.Add(FString::Printf(TEXT("Design airflow %s L/s (from the drawing)"), *Airflow));
	}
	if (!D || D->Telemetry.BuildUnits == 0)
	{
		return;
	}
	const FTwinTelemetry& T = D->Telemetry;
	if (T.Source.IsEmpty())
	{
		if (T.Failures > 0)
		{
			Lines.Add(BadgeLine(TEXT("Live data: cannot reach the feed (press I)"), Amber, WarnText));
			Details.Add(DimLine(FString::Printf(TEXT("Feed problem (%d tries): %s"), T.Failures, *T.Error.Left(160))));
		}
		else
		{
			Lines.Add(FTwinHudLine(TEXT("Live data: waiting for the first reading"), TextDim));
		}
		return;
	}
	// where the numbers come from, as plainly as the web app says it: measured or a demonstration
	const FString Source = SourceOf(D, Sel);
	if (Source == TEXT("live"))
	{
		Lines.Add(BadgeLine(TEXT("Data: LIVE BMS (measured)"), Green, LiveText, TEXT("")));
	}
	else if (Source == TEXT("simulated"))
	{
		Lines.Add(BadgeLine(TEXT("Data: SIMULATED BMS – demonstration, not measured"), Amber, SimText));
	}
	else if (Source == TEXT("mixed"))
	{
		Lines.Add(BadgeLine(TEXT("Data: LIVE BMS on some floors, SIMULATED (not measured) on others"), Amber, SimText));
	}
	else
	{
		Lines.Add(BadgeLine(TEXT("Data: not available right now (press I)"), Amber, WarnText));
		Details.Add(DimLine(FString::Printf(TEXT("Feed: %s"), *(T.Error.IsEmpty() ? T.Label : T.Error.Left(160)))));
		return;
	}
	Details.Add(DimLine(FString::Printf(TEXT("Feed: %s"), *T.Label)));
	const FTwinHvacReading* R = UnitId.IsEmpty() ? nullptr : D->ReadingFor(UnitId);
	if (!R)
	{
		Lines.Add(FTwinHudLine(TEXT("No reading for this unit"), TextDim));
		return;
	}
	TArray<FString> Status;
	Status.Add(R->bOn ? (bUnit ? TEXT("Running") : TEXT("Unit running")) : (bUnit ? TEXT("Off") : TEXT("Unit off")));
	const FString Mode = ModeWord(R->Mode);
	if (R->bOn && !Mode.IsEmpty())
	{
		Status.Add(Mode);
	}
	if (!FMath::IsNaN(R->SetpointC))
	{
		Status.Add(FString::Printf(TEXT("set to %s"), *TempShort(R->SetpointC)));
	}
	Lines.Add(FString::Join(Status, Sep));
	TArray<FString> Air;
	if (R->bOn && !FMath::IsNaN(R->SupplyC))
	{
		Air.Add(FString::Printf(TEXT("Supply air %s"), *Temp(R->SupplyC)));
	}
	if (!FMath::IsNaN(R->ReturnC))
	{
		Air.Add(FString::Printf(TEXT("%s %s"), Air.Num() ? TEXT("return air") : TEXT("Return air"), *Temp(R->ReturnC)));
	}
	if (!FMath::IsNaN(R->RoomC))
	{
		Air.Add(FString::Printf(TEXT("%s %s"), Air.Num() ? TEXT("room") : TEXT("Room"), *Temp(R->RoomC)));
	}
	if (Air.Num())
	{
		Lines.Add(FString::Join(Air, Sep));
	}
	if (R->bOn)
	{
		TArray<FString> Fan;
		if (!FMath::IsNaN(R->Fan))
		{
			Fan.Add(FString::Printf(TEXT("Fan %.0f %%"), R->Fan * 100.f));
		}
		if (!FMath::IsNaN(R->AirflowLps))
		{
			Fan.Add(FString::Printf(TEXT("%s %.0f L/s"), Fan.Num() ? TEXT("airflow") : TEXT("Airflow"), R->AirflowLps));
		}
		if (Fan.Num())
		{
			Lines.Add(FString::Join(Fan, Sep));
		}
	}
	if (!R->Alarm.IsEmpty())
	{
		Lines.Add(BadgeLine(FString::Printf(TEXT("Alarm: %s"), *R->Alarm), Red, ProblemText));
	}
	const double Age = FPlatformTime::Seconds() - T.ReceivedAt;
	if (T.Failures > 0)
	{
		Lines.Add(BadgeLine(FString::Printf(TEXT("Feed interrupted – showing the reading from %s ago"), *Ago(Age)), Amber, WarnText));
		Details.Add(DimLine(FString::Printf(TEXT("Feed problem (%d tries): %s"), T.Failures, *T.Error.Left(160))));
	}
	else
	{
		Lines.Add(FTwinHudLine(FString::Printf(TEXT("Updated %s ago"), *Ago(Age)), TextDim, 0.9f));
	}
}

FBox2D ATwinHUD::DrawSelection(ATwinPlayerController* PC, ATwinViewerDirector* D, AActor* Sel, float Bottom)
{
	DrawBox(Sel->GetComponentsBoundingBox(true), Amber);
	const FString Kind = TwinMeta::Get(Sel, TEXT("Kind"));
	const FString Label = TwinMeta::Get(Sel, TEXT("Label"));
	const FString Type = FriendlyKind(Sel);
	TArray<FTwinHudLine> L, Details;
	L.Add(TitleLine(Label.IsEmpty() ? Type : Label));
	TArray<FString> What = {Type};
	const FString Room = TwinMeta::Get(Sel, TEXT("RoomName"));
	if (!Room.IsEmpty() && Room != Label)
	{
		What.Add(Room);
	}
	L.Add(FString::Join(What, Sep));
	const FString Size = TwinMeta::Get(Sel, TEXT("Size"));
	if (!Size.IsEmpty())
	{
		L.Add(FString::Printf(TEXT("Size %s"), *PrettySize(Size)));
	}
	HvacLines(D, Sel, L, Details);
	if (PC->bShowDetails)
	{
		L.Add(SectionLine(TEXT("TECHNICAL DETAILS")));
		TArray<FString> Ids = {FString::Printf(TEXT("type %s"), *Kind)};
		static const TCHAR* const IdTags[][2] = {{TEXT("Asset"), TEXT("asset")}, {TEXT("Id"), TEXT("id")}, {TEXT("Category"), TEXT("category")},
		                                         {TEXT("Hvac"), TEXT("hvac id")}, {TEXT("Unit"), TEXT("unit id")}, {TEXT("UnitKind"), TEXT("unit kind")},
		                                         {TEXT("System"), TEXT("system")}, {TEXT("Room"), TEXT("room id")}, {TEXT("Floor"), TEXT("floor")}};
		for (const auto& K : IdTags)
		{
			const FString V = TwinMeta::Get(Sel, K[0]);
			if (!V.IsEmpty())
			{
				Ids.Add(FString::Printf(TEXT("%s %s"), K[1], *V));
			}
		}
		L.Add(DimLine(FString::Join(Ids, Sep)));
		L.Append(Details);
		L.Add(DimLine(FString::Printf(TEXT("Object %s"), *TwinMeta::Get(Sel, TEXT("Name")))));
	}
	const FVector2D S = LayoutPanel(L, 280.f, FMath::Min(480.f, Canvas->ClipX * 0.42f));
	return DrawPanel(Canvas->ClipX - Margin - S.X, FMath::Max(Margin, Bottom - (float)S.Y), S, L, Amber);
}

// ------------------------------------------------------------------------------------------------
// controls: hint bar and the full card
// ------------------------------------------------------------------------------------------------

FBox2D ATwinHUD::DrawHintBar(ATwinPlayerController* PC, ATwinViewerDirector* D, float Y, float H)
{
	struct FHint
	{
		FString Keys;
		FString Text;
		int32 Priority;  // 0 always shown; higher numbers go first when the window is too narrow
	};
	TArray<FHint> Items;
	Items.Add({TEXT("W|A|S|D"), TEXT("move"), 0});
	Items.Add({TEXT("Mouse"), TEXT("look"), 0});
	if (PC->RoomFloors.Num() > 0)
	{
		Items.Add({TEXT("M"), TEXT("rooms"), 1});
	}
	if (D && D->IsMultiFloor())
	{
		Items.Add({TEXT("PgUp|/|PgDn"), TEXT("floors"), 1});
	}
	if (HasAirflow(D))
	{
		Items.Add({TEXT("V"), D->IsAirflowVisible() ? TEXT("airflow off") : TEXT("airflow"), 1});
		if (D->IsAirflowVisible())
		{
			Items.Add({TEXT("T"), TEXT("mist"), 3});
		}
	}
	if (PC->Selected.IsValid())
	{
		Items.Add({TEXT("X"), TEXT("clear selection"), 2});
	}
	Items.Add({TEXT("H"), TEXT("all controls"), 0});

	UFont* Font = GEngine->GetMediumFont();
	const float Gap = 24.f, Pad = 14.f;
	auto ItemW = [&](const FHint& I)
	{
		float TW = 0.f, TH = 0.f;
		GetTextSize(I.Text, TW, TH, Font, HelpTextScale);
		return Keys(0.f, 0.f, I.Keys, false) + 6.f + TW;
	};
	auto Total = [&]()
	{
		float W = 4.f + 2.f * Pad;
		for (int32 i = 0; i < Items.Num(); ++i)
		{
			W += ItemW(Items[i]) + (i > 0 ? Gap : 0.f);
		}
		return W;
	};
	while (Total() > Canvas->ClipX - 2.f * Margin)
	{
		int32 Drop = INDEX_NONE;
		for (int32 i = 0; i < Items.Num(); ++i)
		{
			if (Items[i].Priority > 0 && (Drop == INDEX_NONE || Items[i].Priority >= Items[Drop].Priority))
			{
				Drop = i;
			}
		}
		if (Drop == INDEX_NONE)
		{
			break;
		}
		Items.RemoveAt(Drop);
	}
	const float W = Total();
	const float X = (Canvas->ClipX - W) * 0.5f;
	DrawRect(PanelBg, X, Y, W, H);
	DrawRect(Blue, X, Y, 4.f, H);
	float CX = X + 4.f + Pad;
	const float KY = Y + (H - KeyH) * 0.5f;
	for (int32 i = 0; i < Items.Num(); ++i)
	{
		if (i > 0)
		{
			DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.3f), CX + Gap * 0.5f - 1.5f, Y + H * 0.5f - 1.5f, 3.f, 3.f);
			CX += Gap;
		}
		CX += Keys(CX, KY, Items[i].Keys, true) + 6.f;
		float TW = 0.f, TH = 0.f;
		GetTextSize(Items[i].Text, TW, TH, Font, HelpTextScale);
		DrawText(Items[i].Text, TextMain, CX, Y + (H - TH) * 0.5f, Font, HelpTextScale);
		CX += TW;
	}
	const FBox2D R(FVector2D(X, Y), FVector2D(X + W, Y + H));
	Used.Add(R);
	return R;
}

FBox2D ATwinHUD::DrawControlsCard(ATwinPlayerController* PC, ATwinViewerDirector* D, float Left, float Right, float Top, float Bottom)
{
	struct FRow
	{
		FString Keys;
		FString Text;
	};
	struct FSection
	{
		FString Title;
		TArray<FRow> Rows;
		float KeyW = 0.f;
		float TextW = 0.f;
		float H = 0.f;
	};
	const bool bAirflow = HasAirflow(D);
	TArray<FSection> Sections;
	Sections.Add({TEXT("MOVE"), {
		{TEXT("W|A|S|D"), TEXT("Move around")},
		{TEXT("Shift"), TEXT("Hold to go faster")},
		{TEXT("Space"), TEXT("Jump")},
		{TEXT("Right click"), TEXT("Walk to the spot you point at")},
		{TEXT("F"), TEXT("Fly on / off")},
		{TEXT("E|/|Q"), TEXT("Fly up / down")},
		{TEXT("G"), TEXT("Fly through walls on / off")}}});
	Sections.Add({TEXT("LOOK"), {
		{TEXT("Mouse"), TEXT("Look around (arrow keys too)")},
		{TEXT("[|/|]"), FString::Printf(TEXT("Mouse sensitivity (now %.2fx)"), PC->LookSensitivity)}}});
	FSection Explore{TEXT("EXPLORE")};
	Explore.Rows.Add({TEXT("Left click"), TEXT("Select an object to see what it is")});
	Explore.Rows.Add({TEXT("X"), TEXT("Clear the selection")});
	Explore.Rows.Add({TEXT("M"), TEXT("Room list (click a room to go there)")});
	Explore.Rows.Add({TEXT("Tab|or|1-9"), TEXT("Jump to the next room / room 1-9")});
	if (D && D->IsMultiFloor())
	{
		Explore.Rows.Add({TEXT("PgUp|/|PgDn"), TEXT("Floor up / down")});
	}
	if (D)
	{
		Explore.Rows.Add({TEXT("B"), TEXT("Open another building")});
	}
	Sections.Add(Explore);
	FSection Views{TEXT("VIEWS")};
	Views.Rows.Add({TEXT("C"), D && D->IsMultiFloor() ? TEXT("Ceilings (and the floors above) on / off") : TEXT("Ceilings on / off")});
	if (bAirflow)
	{
		Views.Rows.Add({TEXT("V"), TEXT("Airflow illustration on / off")});
		Views.Rows.Add({TEXT("T"), TEXT("Temperature mist on / off")});
	}
	Views.Rows.Add({TEXT("I"), TEXT("Technical details on / off")});
	Sections.Add(Views);
	FSection Other{TEXT("OTHER")};
	Other.Rows.Add({TEXT("H"), TEXT("Show / hide this card")});
	if (PC->bStreaming)
	{
		// streamed into a browser: Esc belongs to the browser, files would land on the server
		Other.Rows.Add({TEXT("Esc"), TEXT("Free the mouse (click the view to steer)")});
	}
	else
	{
		Other.Rows.Add({TEXT("P"), TEXT("Save a screenshot")});
		if (D)
		{
			Other.Rows.Add({TEXT("Ctrl|+|V"), TEXT("Open a copied launch code")});
		}
		Other.Rows.Add({TEXT("Esc"), TEXT("Quit")});
	}
	Sections.Add(Other);

	UFont* Font = GEngine->GetMediumFont();
	const float Pad = 16.f, TitleH = 36.f, HeadH = 24.f, RowH = 26.f, SecGap = 10.f, ColGap = 30.f, KeyGap = 12.f;
	for (FSection& S : Sections)
	{
		for (const FRow& R : S.Rows)
		{
			float TW = 0.f, TH = 0.f;
			GetTextSize(R.Text, TW, TH, Font, HelpTextScale);
			S.KeyW = FMath::Max(S.KeyW, Keys(0.f, 0.f, R.Keys, false));
			S.TextW = FMath::Max(S.TextW, TW);
		}
		S.H = HeadH + S.Rows.Num() * RowH;
	}
	// three columns when there is room, else two, else one (sections in reading order)
	const TArray<TArray<TArray<int32>>> Options = {{{0, 1}, {2}, {3, 4}}, {{0, 1, 4}, {2, 3}}, {{0, 1, 2, 3, 4}}};
	struct FFit
	{
		int32 Option = 0;
		TArray<float> KeyW, ColW;
		float W = 0.f, H = 0.f;
	};
	TArray<FFit> Fits;
	for (int32 o = 0; o < Options.Num(); ++o)
	{
		FFit F;
		F.Option = o;
		float ColsH = 0.f;
		for (const TArray<int32>& Col : Options[o])
		{
			float KW = 0.f, TW = 0.f, CH = 0.f;
			for (int32 s : Col)
			{
				KW = FMath::Max(KW, Sections[s].KeyW);
				TW = FMath::Max(TW, Sections[s].TextW);
				CH += Sections[s].H + (CH > 0.f ? SecGap : 0.f);
			}
			F.KeyW.Add(KW);
			F.ColW.Add(KW + KeyGap + TW);
			F.W += KW + KeyGap + TW;
			ColsH = FMath::Max(ColsH, CH);
		}
		F.W += 4.f + 2.f * Pad + ColGap * (Options[o].Num() - 1);
		F.H = Pad + TitleH + ColsH + Pad - 6.f;
		Fits.Add(F);
	}
	const FFit* Pick = nullptr;
	for (const FFit& F : Fits)
	{
		if (F.W <= Right - Left && F.H <= Bottom - Top)
		{
			Pick = &F;
			break;
		}
	}
	if (!Pick)
	{
		// too small for everything: the widest layout that fits across (it is the shortest)
		for (const FFit& F : Fits)
		{
			if (F.W <= Right - Left)
			{
				Pick = &F;
				break;
			}
		}
	}
	Pick = Pick ? Pick : &Fits.Last();

	const float W = Pick->W, H = Pick->H;
	const float X = Left + FMath::Max(0.f, (Right - Left - W) * 0.5f);
	const float Y = FMath::Max(Top, Bottom - H);
	DrawRect(FLinearColor(0.02f, 0.03f, 0.05f, 0.8f), X, Y, W, H);
	DrawRect(Blue, X, Y, 4.f, H);
	const float CX0 = X + 4.f + Pad;
	DrawText(TEXT("Controls"), FLinearColor::White, CX0, Y + Pad - 4.f, Font, 1.15f);
	const double Left_s = PC->HelpSecondsLeft();
	const FString Note = Left_s >= 0.0 ? FString::Printf(TEXT("Closes in %d s  ·  press H any time to see it again"), FMath::CeilToInt(Left_s))
	                                   : FString(TEXT("Press H to close"));
	float NW = 0.f, NH = 0.f;
	GetTextSize(Note, NW, NH, Font, 0.85f);
	DrawText(Note, TextDim, X + W - Pad - NW, Y + Pad, Font, 0.85f);
	DrawRect(FLinearColor(1.f, 1.f, 1.f, 0.14f), CX0, Y + Pad + TitleH - 12.f, W - 4.f - 2.f * Pad, 1.f);

	float CX = CX0;
	const TArray<TArray<int32>>& Cols = Options[Pick->Option];
	for (int32 c = 0; c < Cols.Num(); ++c)
	{
		float SY = Y + Pad + TitleH;
		for (int32 s : Cols[c])
		{
			const FSection& Sec = Sections[s];
			DrawText(Sec.Title, TextHeading, CX, SY, Font, 0.8f);
			SY += HeadH;
			for (const FRow& R : Sec.Rows)
			{
				Keys(CX, SY, R.Keys, true);
				float TW = 0.f, TH = 0.f;
				GetTextSize(R.Text, TW, TH, Font, HelpTextScale);
				DrawText(R.Text, TextMain, CX + Pick->KeyW[c] + KeyGap, SY + (KeyH - TH) * 0.5f, Font, HelpTextScale);
				SY += RowH;
			}
			SY += SecGap;
		}
		CX += Pick->ColW[c] + ColGap;
	}
	const FBox2D R(FVector2D(X, Y), FVector2D(X + W, Y + H));
	Used.Add(R);
	return R;
}

void ATwinHUD::DrawToast(const FString& Message, const FLinearColor& Accent)
{
	TArray<FTwinHudLine> L = {FTwinHudLine(Message, FLinearColor::White, 1.f)};
	const FVector2D S = LayoutPanel(L, 0.f, FMath::Min(720.f, Canvas->ClipX - 2.f * Margin));
	const float X = (Canvas->ClipX - (float)S.X) * 0.5f;
	float Y = Margin;
	// top centre, moved down below whatever panel it would cover
	for (int32 Pass = 0; Pass < 8; ++Pass)
	{
		const FBox2D R(FVector2D(X, Y), FVector2D(X + S.X, Y + S.Y));
		bool bMoved = false;
		for (const FBox2D& U : Used)
		{
			if (R.Intersect(U) && U.Max.Y + 8.f > Y)
			{
				Y = (float)U.Max.Y + 8.f;
				bMoved = true;
				break;
			}
		}
		if (!bMoved)
		{
			break;
		}
	}
	DrawPanel(X, Y, S, L, Accent);
}

// ------------------------------------------------------------------------------------------------

void ATwinHUD::DrawHUD()
{
	Super::DrawHUD();
	Used.Reset();
	ATwinPlayerController* PC = Cast<ATwinPlayerController>(GetOwningPlayerController());
	if (!PC || !Canvas)
	{
		return;
	}
	ATwinViewerDirector* D = PC->Director.Get();
	if (D && !D->IsReady())
	{
		DrawLoadScreen(PC, D);
		if (PC->Menu != ATwinPlayerController::EMenu::None)
		{
			DrawMenu(PC, D);
		}
		return;
	}
	const bool bMenu = PC->Menu != ATwinPlayerController::EMenu::None;
	const float CX = Canvas->ClipX * 0.5f, CY = Canvas->ClipY * 0.5f;
	if (!bMenu)
	{
		DrawLine(CX - 9.f, CY, CX + 9.f, CY, FLinearColor(1, 1, 1, 0.9f), 2.f);
		DrawLine(CX, CY - 9.f, CX, CY + 9.f, FLinearColor(1, 1, 1, 0.9f), 2.f);
	}

	// building + current room (top left), the airflow banner next to it
	const FBox2D Info = DrawInfoPanel(PC, D);
	FBox2D Banner(ForceInit);
	if (D && D->IsAirflowVisible())
	{
		Banner = DrawAirflowBanner(D, PC->CurrentFloor, Info);
	}

	// selection (bottom right, above the hint bar)
	const float BarH = 30.f;
	const float BarY = Canvas->ClipY - Margin - BarH;
	FBox2D Sel(ForceInit);
	if (AActor* A = PC->Selected.Get())
	{
		Sel = DrawSelection(PC, D, A, BarY - 10.f);
	}

	// controls: the full card (H, and for a few seconds after a load) or the one-line hint bar
	if (!bMenu)
	{
		if (PC->bShowHelp)
		{
			const float Top = FMath::Max((float)Info.Max.Y, Banner.bIsValid ? (float)Banner.Max.Y : 0.f) + 10.f;
			const float Right = Sel.bIsValid ? (float)Sel.Min.X - 14.f : Canvas->ClipX - Margin;
			DrawControlsCard(PC, D, Margin, Right, Top, Canvas->ClipY - Margin);
		}
		else
		{
			DrawHintBar(PC, D, BarY, BarH);
		}
	}

	const double IdleLeft = PC->IdleSecondsLeft();
	if (IdleLeft >= 0.0 && IdleLeft < 60.0)   // streamed session about to close for lack of input
	{
		TArray<FTwinHudLine> L = {FTwinHudLine(FString::Printf(TEXT("No input for a while: this session closes in %d s. Move the mouse to stay."),
		                                                       FMath::CeilToInt(IdleLeft)), FLinearColor::White, 1.f)};
		const FVector2D S = LayoutPanel(L, 0.f, FMath::Min(560.f, Canvas->ClipX - 2.f * Margin));
		DrawPanel(CX - (float)S.X * 0.5f, CY - 24.f - (float)S.Y, S, L, Amber);   // just above the crosshair
	}
	if (!PC->Status.IsEmpty() && GetWorld()->GetTimeSeconds() < PC->StatusUntil)
	{
		DrawToast(PC->Status, Blue);
	}
	if (bMenu)
	{
		DrawMenu(PC, D);
	}
}
