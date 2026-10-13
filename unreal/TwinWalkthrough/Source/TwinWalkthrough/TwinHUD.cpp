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
	const TCHAR* Deg = TEXT("°C");

	FString Temp(float C)
	{
		return FMath::IsNaN(C) ? FString(TEXT("-")) : FString::Printf(TEXT("%.1f %s"), C, Deg);
	}
}

float ATwinHUD::Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent)
{
	UFont* Font = GEngine->GetMediumFont();
	const float LineH = 22.f;
	const float H = 14.f + Lines.Num() * LineH;
	DrawRect(FLinearColor(0.02f, 0.03f, 0.05f, 0.62f), X, Y, W, H);
	DrawRect(Accent, X, Y, 4.f, H);
	for (int32 i = 0; i < Lines.Num(); ++i)
	{
		DrawText(Lines[i], i == 0 ? FLinearColor::White : FLinearColor(0.85f, 0.87f, 0.9f), X + 14.f, Y + 8.f + i * LineH,
		         Font, i == 0 ? 1.05f : 0.95f);
	}
	return H;
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
		// wrap the message at ~95 characters
		FString Msg = D->ErrorMessage;
		while (Msg.Len() > 95)
		{
			int32 Cut = 95;
			while (Cut > 40 && Msg[Cut] != TEXT(' '))
			{
				--Cut;
			}
			Lines.Add(Msg.Left(Cut));
			Msg = Msg.RightChop(Cut + 1);
		}
		Lines.Add(Msg);
		Lines.Add(TEXT(""));
		if (D->ErrorCode.StartsWith(TEXT("TOKEN_")))
		{
			Lines.Add(TEXT("The viewer link is not valid any more: open the building again from the web app."));
		}
		Lines.Add(TEXT("R retry  |  B choose another building  |  Ctrl+V paste a new launch code  |  Esc quit"));
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

void ATwinHUD::HvacLines(ATwinViewerDirector* D, const AActor* Sel, TArray<FString>& Lines)
{
	const FString Kind = TwinMeta::Get(Sel, TEXT("Kind"));
	if (!Kind.StartsWith(TEXT("hvac_")))
	{
		return;
	}
	const FString Tag = TwinMeta::Get(Sel, TEXT("Tag"));
	const FString UnitId = TwinMeta::Get(Sel, TEXT("Unit"));
	if (Kind == TEXT("hvac_unit"))
	{
		Lines.Add(FString::Printf(TEXT("Unit tag: %s  (%s, %s)"), *Tag, *UnitId, *TwinMeta::Get(Sel, TEXT("UnitKind"))));
	}
	else if (!Tag.IsEmpty())
	{
		Lines.Add(FString::Printf(TEXT("Served by unit %s"), *Tag));
	}
	const FString Airflow = TwinMeta::Get(Sel, TEXT("Airflow"));
	if (!Airflow.IsEmpty())
	{
		Lines.Add(FString::Printf(TEXT("Design airflow (drawing): %s L/s"), *Airflow));
	}
	if (!D || D->Telemetry.BuildUnits == 0)
	{
		return;
	}
	const FTwinTelemetry& T = D->Telemetry;
	if (T.Source.IsEmpty())
	{
		Lines.Add(TEXT("Live data: waiting for the first reading"));
		return;
	}
	Lines.Add(FString::Printf(TEXT("Data: %s"), *(T.Source == TEXT("live") ? FString(TEXT("LIVE BMS (measured)"))
	                                                : T.Source == TEXT("simulated") ? FString(TEXT("SIMULATED BMS - demonstration, not measured"))
	                                                : FString::Printf(TEXT("unavailable %s"), *T.Error))));
	const FTwinHvacReading* R = UnitId.IsEmpty() ? nullptr : D->ReadingFor(UnitId);
	if (!R)
	{
		if (T.Source != TEXT("unavailable"))
		{
			Lines.Add(TEXT("No reading for this unit"));
		}
		return;
	}
	if (!R->bOn)
	{
		Lines.Add(FString::Printf(TEXT("Status: OFF  |  setpoint %s  |  return %s"), *Temp(R->SetpointC), *Temp(R->ReturnC)));
	}
	else
	{
		Lines.Add(FString::Printf(TEXT("Status: ON (%s)  |  setpoint %s"), *R->Mode, *Temp(R->SetpointC)));
		Lines.Add(FString::Printf(TEXT("Supply air %s  |  return air %s"), *Temp(R->SupplyC), *Temp(R->ReturnC)));
		Lines.Add(FString::Printf(TEXT("Fan %.0f %%  |  airflow %.0f L/s"), FMath::IsNaN(R->Fan) ? 0.f : R->Fan * 100.f,
		                          FMath::IsNaN(R->AirflowLps) ? 0.f : R->AirflowLps));
	}
	if (!R->Alarm.IsEmpty())
	{
		Lines.Add(FString::Printf(TEXT("ALARM: %s"), *R->Alarm));
	}
	const double Age = FPlatformTime::Seconds() - T.ReceivedAt;
	Lines.Add(FString::Printf(TEXT("Updated %.0f s ago%s"), Age, T.Failures > 0 ? TEXT(" (feed interrupted, showing the last reading)") : TEXT("")));
}

void ATwinHUD::DrawAirflowBanner(ATwinViewerDirector* D)
{
	TArray<FString> Lines;
	Lines.Add(TEXT("AIRFLOW ILLUSTRATION  -  schematic, not measured, not a CFD result"));
	Lines.Add(TEXT("Paths: drawn from diffuser / grille positions, throw patterns and design airflow"));
	const FTwinTelemetry& T = D->Telemetry;
	Lines.Add(T.Source.IsEmpty() || T.Source == TEXT("unavailable")
		          ? FString(TEXT("Colour: no temperature data (grey)"))
		          : FString::Printf(TEXT("Colour: supply -> return air temperature from %s"), *T.Label));
	if (D->Airflow.IsValid())
	{
		const TSharedPtr<FJsonObject>* Th = nullptr;
		bool bAvail = false;
		if (D->Airflow->TryGetObjectField(TEXT("thermal"), Th) && (*Th)->TryGetBoolField(TEXT("available"), bAvail) && bAvail)
		{
			Lines.Add(FString::Printf(TEXT("MATLAB thermal model (simulation): west %s | east %s | setpoint %.0f %s | minute %d/%d"),
			                          *Temp((float)(*Th)->GetNumberField(TEXT("west_c"))), *Temp((float)(*Th)->GetNumberField(TEXT("east_c"))),
			                          (*Th)->GetNumberField(TEXT("setpoint_c")), Deg, (int32)(*Th)->GetNumberField(TEXT("minute")),
			                          (int32)(*Th)->GetNumberField(TEXT("minutes"))));
		}
	}
	// centred, but never over the room panel on the left (18 + 470 px)
	const float W = FMath::Min(760.f, Canvas->ClipX - 512.f);
	const float X = FMath::Max((Canvas->ClipX - W) * 0.5f, 500.f);
	const float H = Panel(X, 18.f, W, Lines, Amber);
	// legend
	const float LY = 18.f + H + 6.f;
	const float Steps[] = {12.f, 15.f, 18.f, 21.f, 24.f, 27.f, 30.f};
	const float SW = W / UE_ARRAY_COUNT(Steps);
	for (int32 i = 0; i < UE_ARRAY_COUNT(Steps); ++i)
	{
		DrawRect(ATwinAirflowActor::TemperatureColor(Steps[i]), X + i * SW, LY, SW, 14.f);
		DrawText(FString::Printf(TEXT("%.0f %s"), Steps[i], Deg), FLinearColor::White, X + i * SW + 4.f, LY + 16.f, GEngine->GetSmallFont());
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
			                                        *TwinMeta::Get(F.Get(), TEXT("RoomName")), *TwinMeta::Get(F.Get(), TEXT("Area")))
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

void ATwinHUD::DrawHUD()
{
	Super::DrawHUD();
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
	const float CX = Canvas->ClipX * 0.5f, CY = Canvas->ClipY * 0.5f;
	if (PC->Menu == ATwinPlayerController::EMenu::None)
	{
		DrawLine(CX - 9.f, CY, CX + 9.f, CY, FLinearColor(1, 1, 1, 0.9f), 2.f);
		DrawLine(CX, CY - 9.f, CX, CY + 9.f, FLinearColor(1, 1, 1, 0.9f), 2.f);
	}

	// project + current room
	TArray<FString> Top;
	const AActor* Info = PC->ProjectInfo.Get();
	Top.Add(Info ? TwinMeta::Get(Info, TEXT("Name")) : FString(TEXT("Floor Plan Twin")));
	if (const AActor* Floor = PC->CurrentRoomFloor.Get())
	{
		Top.Add(FString::Printf(TEXT("Room: %s"), *TwinMeta::Get(Floor, TEXT("RoomName"))));
		Top.Add(FString::Printf(TEXT("Clear size %s  |  %s"), *TwinMeta::Get(Floor, TEXT("Dims")), *TwinMeta::Get(Floor, TEXT("Area"))));
		Top.Add(FString::Printf(TEXT("Floor: %s"), *TwinMeta::Get(Floor, TEXT("FloorMaterial"))));
	}
	else
	{
		Top.Add(TEXT("Outside the rooms"));
	}
	Top.Add(FString::Printf(TEXT("Mode: %s"), *PC->ModeName()));
	FLinearColor TopAccent = Blue;
	if (D)
	{
		Top.Add(D->ValidationFailures == 0
			        ? FString::Printf(TEXT("Model check: %d objects match the floor plan (max %.2f cm)"), D->ValidationChecked, D->MaxDeviationCm)
			        : FString::Printf(TEXT("Model check: %d PROBLEMS (see Saved/TwinViewer)"), D->ValidationFailures));
		TopAccent = D->ValidationFailures == 0 ? Blue : Red;
		const FTwinTelemetry& T = D->Telemetry;
		if (T.BuildUnits > 0)
		{
			if (T.Source.IsEmpty())
			{
				Top.Add(TEXT("HVAC data: connecting..."));
			}
			else
			{
				Top.Add(FString::Printf(TEXT("HVAC data: %s  |  %d/%d units"), *T.Label, T.Units.Num(), T.BuildUnits));
			}
			if (T.Failures > 0)
			{
				Top.Add(FString::Printf(TEXT("HVAC feed interrupted (%d tries): %s"), T.Failures, *T.Error.Left(70)));
			}
		}
	}
	const bool bAirflow = D && D->IsAirflowVisible();
	Panel(18.f, bAirflow ? 18.f : 18.f, 470.f, Top, TopAccent);
	if (bAirflow)
	{
		DrawAirflowBanner(D);
	}

	// selection
	if (AActor* Sel = PC->Selected.Get())
	{
		DrawBox(Sel->GetComponentsBoundingBox(true), Amber);
		TArray<FString> Lines;
		const FString Label = TwinMeta::Get(Sel, TEXT("Label"));
		Lines.Add(Label.IsEmpty() ? TwinMeta::Get(Sel, TEXT("Name")) : Label);
		Lines.Add(FString::Printf(TEXT("Type: %s"), *TwinMeta::Get(Sel, TEXT("Kind"))));
		const FString Size = TwinMeta::Get(Sel, TEXT("Size"));
		if (!Size.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Size: %s"), *Size));
		}
		const FString Asset = TwinMeta::Get(Sel, TEXT("Asset"));
		if (!Asset.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Asset: %s  (%s)"), *Asset, *TwinMeta::Get(Sel, TEXT("Id"))));
		}
		const FString Room = TwinMeta::Get(Sel, TEXT("RoomName"));
		if (!Room.IsEmpty())
		{
			Lines.Add(FString::Printf(TEXT("Room: %s"), *Room));
		}
		HvacLines(D, Sel, Lines);
		Lines.Add(TwinMeta::Get(Sel, TEXT("Name")));
		Panel(Canvas->ClipX - 520.f, Canvas->ClipY - 40.f - Lines.Num() * 22.f, 502.f, Lines, Amber);
	}

	// help / status
	if (PC->bShowHelp && PC->Menu == ATwinPlayerController::EMenu::None)
	{
		TArray<FString> Help = {
			TEXT("Controls"),
			TEXT("WASD move | mouse/arrows look | Shift run | Space jump"),
			TEXT("F fly (E/Q up/down) | G ghost | C ceilings | Tab / 1-9 rooms | M room list"),
			TEXT("Left click select | Right click walk there | X clear"),
		};
		if (D)
		{
			Help.Add(TEXT("V airflow illustration | B switch building | Ctrl+V paste launch code"));
		}
		Help.Add(TEXT("P screenshot | H hide help | Esc quit"));
		Panel(18.f, Canvas->ClipY - 36.f - Help.Num() * 22.f, 600.f, Help, Blue);
	}
	if (!PC->Status.IsEmpty() && GetWorld()->GetTimeSeconds() < PC->StatusUntil)
	{
		Panel(CX - 260.f, bAirflow ? Canvas->ClipY - 80.f : 24.f, 520.f, {PC->Status}, Blue);
	}
	if (PC->Menu != ATwinPlayerController::EMenu::None)
	{
		DrawMenu(PC, D);
	}
}
